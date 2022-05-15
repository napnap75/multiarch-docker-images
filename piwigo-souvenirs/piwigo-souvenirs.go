package main

import (
	"context"
	"database/sql"
	"flag"
	"fmt"
	"io/ioutil"
	"os"
	"os/signal"
	"strings"
	"time"
	_ "github.com/go-sql-driver/mysql"
	"github.com/mdp/qrterminal/v3"
	_ "github.com/mattn/go-sqlite3"
	"google.golang.org/protobuf/proto"
	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/store/sqlstore"
	waProto "go.mau.fi/whatsmeow/binary/proto"
	waLog "go.mau.fi/whatsmeow/util/log"
)

type parameters struct {
	mysqlURL string
	whatsappSessionFile string
	whatsappGroup string
	piwigoImageFolder string
	piwigoBaseURL string
}

func loadParameters() (parameters) {
	param := new(parameters)
	flag.StringVar(&param.mysqlURL, "mysql-url", "", "The full url of the MySQL server to connect to")
	flag.StringVar(&param.whatsappSessionFile, "whatsapp-session-file", "", "The file to save the WhatsApp session to")
	flag.StringVar(&param.whatsappGroup, "whatsapp-group", "", "The ID of the WhatsApp group to send the message to")
	flag.StringVar(&param.piwigoImageFolder, "piwigo-image-folder", "", "The folder where the Piwigo images are stored")
	flag.StringVar(&param.piwigoBaseURL, "piwigo-base-url", "", "The base url of the Piwigo server")
	flag.Parse()
	return *param
}

func connect(param parameters) (*whatsmeow.Client, error) {
	dbLog := waLog.Stdout("Database", "ERROR", true)
	container, err := sqlstore.New("sqlite3", "file:" + param.whatsappSessionFile + "?_foreign_keys=on", dbLog)
	if err != nil {
		return nil, err
	}
	deviceStore, err := container.GetFirstDevice()
	if err != nil {
		return nil, err
	}
	clientLog := waLog.Stdout("Client", "ERROR", true)
	client := whatsmeow.NewClient(deviceStore, clientLog)

	if client.Store.ID == nil {
		// No ID stored, new login
		qrChan, _ := client.GetQRChannel(context.Background())
		err = client.Connect()
		if err != nil {
			return nil, err
		}
		for evt := range qrChan {
			if evt.Event == "code" {
				qrterminal.GenerateHalfBlock(evt.Code, qrterminal.L, os.Stdout)
				fmt.Println("QR code:", evt.Code)
			} else {
				fmt.Println("Login event:", evt.Event)
			}
		}
	} else {
		// Already logged in, just connect
		err = client.Connect()
		if err != nil {
			return nil, err
		}
	}

	return client, nil
}

func sendMessage(client *whatsmeow.Client, group string, message string, title string, thumbnail []byte) error {
	jid, err := types.ParseJID(group)
	if err != nil {
		return fmt.Errorf("Incorrect group identifier '%s': %v", group, err)
	}

	msg := &waProto.Message{ExtendedTextMessage: &waProto.ExtendedTextMessage{
		Text:          proto.String(message),
		Description:   proto.String(title),
		JpegThumbnail: thumbnail,
	}}
	ts, err := client.SendMessage(jid, "", msg)
	if err != nil {
		return fmt.Errorf("Error sending message with title '%s': %v", title, err)
	}

	fmt.Fprintf(os.Stdout, "Message with title '%s' sent (timestamp: %s)\n", title, ts)
	return nil
}

func testConnexions(param parameters) error {
	// Create new WhatsApp connection and connect
	client, err := connect(param)
	if err != nil {
		return fmt.Errorf("Error connecting to WhatsApp: %v", err)
	}
	<-time.After(3 * time.Second)
	defer client.Disconnect()

	// Prints the available groups if none provided
	if param.whatsappGroup == "" {
		fmt.Fprintf(os.Stdout, "No WhatsApp group provided, showing all available groups\n")
		groups, err := client.GetJoinedGroups()
		if err != nil {
			return fmt.Errorf("Error getting groups list: %v", err)
		}
		for _, groupInfo := range groups {
			fmt.Fprintf(os.Stdout, "%s | %s\n", groupInfo.JID, groupInfo.GroupName)
		}

		return fmt.Errorf("No WhatsApp group provided")
	} else {
		jid, err := types.ParseJID(param.whatsappGroup)
		if err != nil {
			return fmt.Errorf("Incorrect group identifier '%s': %v", param.whatsappGroup, err)
		}
		_, err = client.GetGroupInfo(jid)
		if err != nil {
			return fmt.Errorf("Unknown WhatsApp group %s", param.whatsappGroup)
		}
	}

	// Connect to MySQL and execute a test query
	db, err := sql.Open("mysql", param.mysqlURL + "?parseTime=true")
	if err != nil {
		return fmt.Errorf("Error connecting to MySQL: %v", err)
	}
	defer db.Close()
	results, err := db.Query("SELECT Version();")
	if err != nil {
		return fmt.Errorf("Error executing MySQL query: %v", err)
	}
	defer results.Close()

	// Check the existence of the piwigo thumbnails directory
	_, err = os.Stat(fmt.Sprintf("%s/galleries", param.piwigoImageFolder))
	if err != nil {
		return fmt.Errorf("Could not find Piwigo thumbnail directory: %v", err)
	}

	return nil
}

func runLoop(param parameters) error {
	// Create new WhatsApp connection and connect
	client, err := connect(param)
	if err != nil {
		return fmt.Errorf("Error creating connection to WhatsApp: %v", err)
	}
	<-time.After(3 * time.Second)
	defer client.Disconnect()

	// Connect to MySQL and execute the first query
	db, err := sql.Open("mysql", param.mysqlURL + "?parseTime=true")
	if err != nil {
		return fmt.Errorf("Error connecting to MySQL: %v", err)
	}
	defer db.Close()
	results, err := db.Query("SELECT piwigo_sharealbum.code, piwigo_categories.name, representatives.path, representatives.representative_ext, MIN(piwigo_images.date_creation) AS date_creation FROM piwigo_sharealbum JOIN piwigo_categories ON piwigo_sharealbum.cat = piwigo_categories.id JOIN piwigo_image_category ON piwigo_image_category.category_id = piwigo_categories.id JOIN piwigo_images ON piwigo_image_category.image_id = piwigo_images.id JOIN piwigo_images AS representatives ON piwigo_categories.representative_picture_id = representatives.id WHERE piwigo_images.date_creation IS NOT NULL GROUP BY piwigo_categories.id;")
	if err != nil {
		return fmt.Errorf("Error executing MySQL query: %v", err)
	}
	defer results.Close()

	var albumCode string
	var albumName string
	var representativePath string
	var representativeExt sql.NullString
	var albumDate time.Time
	for results.Next() {
		err = results.Scan(&albumCode, &albumName, &representativePath, &representativeExt, &albumDate)
		if err != nil {
			fmt.Fprintf(os.Stderr, "Error retrieving MySQL results for album '%s': %v\n", albumName, err)
			continue
		}

		if (albumDate.Month() == time.Now().Month()) && (albumDate.Day() == time.Now().Day()) {
			// Prepare the message
			url := fmt.Sprintf("%s/?xauth=%s", param.piwigoBaseURL, albumCode)
			imagePath := representativePath
			if (strings.HasPrefix(imagePath, "./")) {
				imagePath = imagePath[2:len(imagePath)]
			}
			imageExt := imagePath[strings.LastIndex(imagePath, ".")+1:len(imagePath)]
			if representativeExt.Valid {
				imageExt = representativeExt.String
				imagePath = imagePath[0:strings.LastIndex(imagePath, "/")] + "/pwg_representative" + imagePath[strings.LastIndex(imagePath, "/"):len(imagePath)]
			}
			imagePath = imagePath[0:strings.LastIndex(imagePath, ".")] + "-th." + imageExt
			thumbnail, err := ioutil.ReadFile(fmt.Sprintf("%s/%s", param.piwigoImageFolder, imagePath))
			if err != nil {
				fmt.Fprintf(os.Stderr, "Error reading thumbnail for album '%s': %v\n", albumName, err)
				continue
			}

			// Send the message
			sendMessage(client, param.whatsappGroup, fmt.Sprintf("Il y a %d an(s) : %s", time.Now().Year()-albumDate.Year(), url), albumName, thumbnail)
			if err != nil {
				fmt.Fprintf(os.Stderr, "Error sending message to WhatsApp for album '%s': %v\n", albumName, err)
				continue
			}
		}
	}

	// Connect to MySQL and execute the second query
	results, err = db.Query("SELECT piwigo_sharealbum.code, piwigo_categories.name, representatives.path, representatives.representative_ext, piwigo_sharealbum.creation_date AS date_creation FROM piwigo_sharealbum JOIN piwigo_categories ON piwigo_sharealbum.cat = piwigo_categories.id JOIN piwigo_image_category ON piwigo_image_category.category_id = piwigo_categories.id JOIN piwigo_images ON piwigo_image_category.image_id = piwigo_images.id JOIN piwigo_images AS representatives ON piwigo_categories.representative_picture_id = representatives.id WHERE piwigo_sharealbum.creation_date IS NOT NULL GROUP BY piwigo_sharealbum.id;")
	if err != nil {
		return fmt.Errorf("Error executing MySQL query: %v", err)
	}
	defer results.Close()

	var shareDate time.Time
	for results.Next() {
		err = results.Scan(&albumCode, &albumName, &representativePath, &representativeExt, &shareDate)
		if err != nil {
			fmt.Fprintf(os.Stderr, "Error retrieving MySQL results for album '%s': %v\n", albumName, err)
			continue
		}

		if (shareDate.Year() == time.Now().AddDate(0, 0, -1).Year()) && (shareDate.Month() == time.Now().AddDate(0, 0, -1).Month()) && (shareDate.Day() == time.Now().AddDate(0, 0, -1).Day()) {
			// Prepare the message
			url := fmt.Sprintf("%s/?xauth=%s", param.piwigoBaseURL, albumCode)
			imagePath := representativePath
			if (strings.HasPrefix(imagePath, "./")) {
				imagePath = imagePath[2:len(imagePath)]
			}
			imageExt := imagePath[strings.LastIndex(imagePath, ".")+1:len(imagePath)]
			if representativeExt.Valid {
				imageExt = representativeExt.String
				imagePath = imagePath[0:strings.LastIndex(imagePath, "/")] + "/pwg_representative" + imagePath[strings.LastIndex(imagePath, "/"):len(imagePath)]
			}
			imagePath = imagePath[0:strings.LastIndex(imagePath, ".")] + "-th." + imageExt
			thumbnail, err := ioutil.ReadFile(fmt.Sprintf("%s/%s", param.piwigoImageFolder, imagePath))
			if err != nil {
				fmt.Fprintf(os.Stderr, "Error reading thumbnail for album '%s': %v\n", albumName, err)
				continue
			}

			// Send the message
			sendMessage(client, param.whatsappGroup, fmt.Sprintf("Nouvel album : %s", url), albumName, thumbnail)
			if err != nil {
				fmt.Fprintf(os.Stderr, "Error sending message to WhatsApp for album '%s': %v\n", albumName, err)
				continue
			}
		}
	}

	return nil
}

func main() {
	// Handle interrupts to clean properly
	c := make(chan os.Signal)
	signal.Notify(c, os.Interrupt)
	go func() {
		select {
			case sig := <-c:
				fmt.Printf("Got %s signal. Aborting...\n", sig)
				os.Exit(1)
		}
	}()

	// Load the parameters
	param := loadParameters()

	// Test the connexion on startup
	err := testConnexions(param)
	if err != nil {
		fmt.Fprintf(os.Stderr, "Unable to connect: %v\n", err)
		return
	}

	// Run the loop everyday at 7
	for {
		t := time.Now()
		n := time.Date(t.Year(), t.Month(), t.Day(), 7, 0, 0, 0, t.Location())
		d := n.Sub(t)
		if d < 0 {
			n = n.Add(24 * time.Hour)
			d = n.Sub(t)
		}
		fmt.Fprintf(os.Stderr, "Sleeping for: %s\n", d)
		time.Sleep(d)

		err := runLoop(param)
		if err != nil {
			fmt.Fprintf(os.Stderr, "Error: %v\n", err)
			continue
		}
	}
}
