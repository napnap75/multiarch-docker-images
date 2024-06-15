package main

import (
	"bytes"
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"io/ioutil"
	"os"
	"os/signal"
	"net/http"
	"time"
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
	immichURL string
	immichKey string
	whatsappSessionFile string
	whatsappGroup string
	runOnce bool
}

func loadParameters() (parameters) {
	param := new(parameters)
	flag.StringVar(&param.immichURL, "immich-url", "", "The url of the Immich server to connect to")
	flag.StringVar(&param.immichKey, "immich-key", "", "The API KEY to use with Immich")
	flag.StringVar(&param.whatsappSessionFile, "whatsapp-session-file", "", "The file to save the WhatsApp session to")
	flag.StringVar(&param.whatsappGroup, "whatsapp-group", "", "The ID of the WhatsApp group to send the message to")
	flag.BoolVar(&param.runOnce, "run-once", false, "Run once and exits (default to false)")
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

func sendMessage(client *whatsmeow.Client, group string, message string, url string, title string, thumbnail []byte) error {
	jid, err := types.ParseJID(group)
	if err != nil {
		return fmt.Errorf("Incorrect group identifier '%s': %v", group, err)
	}

	msg := &waProto.Message{ExtendedTextMessage: &waProto.ExtendedTextMessage{
		Text:          proto.String(message),
		Title:         proto.String(title),
		Description:   proto.String(title),
		CanonicalURL:  proto.String(url),
		MatchedText:   proto.String(url),
		JPEGThumbnail: thumbnail,
	}}
	ts, err := client.SendMessage(context.Background(), jid, msg)
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

	// Connects to Immich and load albums
	spaceClient := http.Client{
		Timeout: time.Second * 10,
	}
	req, err := http.NewRequest(http.MethodGet, param.immichURL + "/api/albums", nil)
	if err != nil {
		return fmt.Errorf("Error connecting to Immich with URL '%s': %v", param.immichURL, err)
	}
	req.Header.Set("x-api-key", param.immichKey)
	req.Header.Set("Accept", "application/json")

	res, err := spaceClient.Do(req)
	if err != nil {
		return fmt.Errorf("Error connecting to Immich with URL '%s': %v", param.immichURL, err)
	}
	if res.Body != nil {
		defer res.Body.Close()
	}
	if res.StatusCode != 200 {
		return fmt.Errorf("Error connecting to Immich with URL '%s': Status code %d", param.immichURL, res.StatusCode)
	}
	body, err := ioutil.ReadAll(res.Body)
	if err != nil {
		return fmt.Errorf("Error connecting to Immich with URL '%s': %v", param.immichURL, err)
	}
	var albums []map[string]interface{}
	err = json.Unmarshal([]byte(body), &albums)
	if err != nil {
		return fmt.Errorf("Error connecting to Immich with URL '%s': %v", param.immichURL, err)
	}

	return nil
}

func getSharingKey(album map[string]interface{}, param parameters) (string, error) {
	spaceClient := http.Client{
		Timeout: time.Second * 10,
	}
	albumId := album["id"].(string)
	albumName := album["albumName"].(string)
	if (album["hasSharedLink"].(bool)) {
		// Retrieve the existing key
		req, err := http.NewRequest(http.MethodGet, param.immichURL + "/api/shared-links", nil)
		if err != nil {
			return "", fmt.Errorf("Error retrieving sharing key for album '%s': %v", albumName, err)
		}
		req.Header.Set("x-api-key", param.immichKey)
		req.Header.Set("Accept", "application/json")
		res, err := spaceClient.Do(req)
		if err != nil {
			return "", fmt.Errorf("Error retrieving sharing key for album '%s': %v", albumName, err)
		}
		if res.Body != nil {
			defer res.Body.Close()
		}
		if res.StatusCode != 200 {
			return "", fmt.Errorf("Error retrieving sharing key for album '%s': Status code %d", albumName, res.StatusCode)
		}
		body, err := ioutil.ReadAll(res.Body)
		if err != nil {
			return "", fmt.Errorf("Error retrieving sharing key for album '%s': %v", albumName, err)
		}
		var keys []map[string]interface{}
		err = json.Unmarshal([]byte(body), &keys)
		if err != nil {
			return "", fmt.Errorf("Error retrieving sharing key for album '%s': %v", albumName, err)
		}
		for _, key := range keys {
			alb := key["album"].(map[string]interface{})
			if (albumId == alb["id"].(string)) {
				return key["key"].(string), nil
			}
		}

		return "", fmt.Errorf("Error retrieving sharing key for album '%s': no key found for this albume", albumName)
	} else {
		// Create the missing key
		var jsonData = []byte(`{
			"albumId": "` + albumId + `",
			"allowDownload": true,
			"allowUpload": false,
			"showMetadata": true,
			"type": "ALBUM"
		}`)
		req, err := http.NewRequest(http.MethodPost, param.immichURL + "/api/shared-links", bytes.NewBuffer(jsonData))
		if err != nil {
			return "", fmt.Errorf("Error creating missing key for album '%s': %v", albumName, err)
		}
		req.Header.Set("x-api-key", param.immichKey)
		req.Header.Set("Content-Type", "application/json")
		req.Header.Set("Accept", "application/octet-stream")
		res, err := spaceClient.Do(req)
		if err != nil {
			return "", fmt.Errorf("Error creating missing key for album '%s': %v", albumName, err)
		}
		if res.Body != nil {
			defer res.Body.Close()
		}
		if res.StatusCode != 201 {
			return "", fmt.Errorf("Error creating missing key for album '%s': Status code %d", albumName, res.StatusCode)
		}
		body, err := ioutil.ReadAll(res.Body)
		if err != nil {
			return "", fmt.Errorf("Error creating missing key for album '%s': %v", albumName, err)
		}
		var key map[string]interface{}
		err = json.Unmarshal([]byte(body), &key)
		if err != nil {
			return "", fmt.Errorf("Error creating missing key for album '%s': %v", albumName, err)
		}
		return key["key"].(string), nil
	}
}

func getThumbnail(album map[string]interface{}, param parameters) ([]byte, error) {
	spaceClient := http.Client{
		Timeout: time.Second * 10,
	}
	albumName := album["albumName"].(string)

	req, err := http.NewRequest(http.MethodGet, param.immichURL + "/api/assets/" + album["albumThumbnailAssetId"].(string) + "/thumbnail?size=preview", nil)
	if err != nil {
		return nil, fmt.Errorf("Error retrieving thumbnail for album '%s': %v", albumName, err)
	}
	req.Header.Set("x-api-key", param.immichKey)
	req.Header.Set("Accept", "application/octet-stream")
	res, err := spaceClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("Error retrieving thumbnail for album '%s': %v", albumName, err)
	}
	if res.Body != nil {
		defer res.Body.Close()
	}
	if res.StatusCode != 200 {
		return nil, fmt.Errorf("Error retrieving thumbnail for album '%s': Status code %d", albumName, res.StatusCode)
	}
	thumbnail, err := ioutil.ReadAll(res.Body)
	if err != nil {
		return nil, fmt.Errorf("Error retrieving thumbnail for album '%s': %v", albumName, err)
	}

	return thumbnail, nil
}

func runLoop(param parameters) error {
	// Create new WhatsApp connection and connect
	client, err := connect(param)
	if err != nil {
		return fmt.Errorf("Error creating connection to WhatsApp: %v", err)
	}
	<-time.After(3 * time.Second)
	defer client.Disconnect()


	// Connects to Immich and load albums
	spaceClient := http.Client{
		Timeout: time.Second * 10,
	}
	req, err := http.NewRequest(http.MethodGet, param.immichURL + "/api/albums", nil)
	if err != nil {
		return fmt.Errorf("Error connecting to Immich with URL '%s': %v", param.immichURL, err)
	}
	req.Header.Set("x-api-key", param.immichKey)
	req.Header.Set("Accept", "application/json")

	res, err := spaceClient.Do(req)
	if err != nil {
		return fmt.Errorf("Error connecting to Immich with URL '%s': %v", param.immichURL, err)
	}
	if res.Body != nil {
		defer res.Body.Close()
	}
	if res.StatusCode != 200 {
		return fmt.Errorf("Error connecting to Immich with URL '%s': Status code %d", param.immichURL, res.StatusCode)
	}
	body, err := ioutil.ReadAll(res.Body)
	if err != nil {
		return fmt.Errorf("Error connecting to Immich with URL '%s': %v", param.immichURL, err)
	}
	var albums []map[string]interface{}
	err = json.Unmarshal([]byte(body), &albums)
	if err != nil {
		return fmt.Errorf("Error connecting to Immich with URL '%s': %v", param.immichURL, err)
	}

	for _, album := range albums {
		albumName := album["albumName"].(string)
		albumDate, err := time.Parse(time.RFC3339, album["startDate"].(string))
		if err != nil {
			return fmt.Errorf("Incorrect time format received from Immich: %v", err)
		}
		createDate, err := time.Parse(time.RFC3339, album["createdAt"].(string))
		if err != nil {
			return fmt.Errorf("Incorrect time format received from Immich: %v", err)
		}
		isShared := album["shared"].(bool)

		// Get albums from x years ago
		if isShared && (albumDate.Month() == time.Now().Month()) && (albumDate.Day() == time.Now().Day()) {
			// Retrieve the sharing key
			sharingKey, err := getSharingKey(album, param)
			if err != nil {
				return fmt.Errorf("Error retrieving the sharing key for album '%s': %v", albumName, err)
			}

			// Retrieve the thumbnail
			thumbnail, err := getThumbnail(album, param)
			if err != nil {
				return fmt.Errorf("Error retrieving thumbnail for album '%s': %v", albumName, err)
			}

			// Send the message
			link := param.immichURL + "/share/" + sharingKey
			err = sendMessage(client, param.whatsappGroup, fmt.Sprintf("Il y a %d an(s) : %s", time.Now().Year()-albumDate.Year(), link), link, albumName, thumbnail)
			if err != nil {
				fmt.Fprintf(os.Stderr, "Error sending message to WhatsApp for album '%s': %v\n", albumName, err)
				continue
			}
		}

		// Get albums created yesterday
		if isShared && (createDate.Year() == time.Now().AddDate(0, 0, -1).Year()) && (createDate.Month() == time.Now().AddDate(0, 0, -1).Month()) && (createDate.Day() == time.Now().AddDate(0, 0, -1).Day()) {
			// Retrieve the sharing key
			sharingKey, err := getSharingKey(album, param)
			if err != nil {
				return fmt.Errorf("Error retrieving the sharing key for album '%s': %v", albumName, err)
			}

			// Retrieve the thumbnail
			thumbnail, err := getThumbnail(album, param)
			if err != nil {
				return fmt.Errorf("Error retrieving thumbnail for album '%s': %v", albumName, err)
			}

			// Send the message
			link := param.immichURL + "/share/" + sharingKey
			err = sendMessage(client, param.whatsappGroup, fmt.Sprintf("Nouvel album : %s", link), link, albumName, thumbnail)
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
	if param.runOnce {
		err := runLoop(param)
		if err != nil {
			fmt.Fprintf(os.Stderr, "Error: %v\n", err)
		}
	} else {
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
}
