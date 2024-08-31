package main

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io/ioutil"
	"math"
	"net/http"
	"os"
	"os/signal"
	"strconv"
	"strings"
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

type Parameters struct {
	ImmichURL		string
	ImmichKey		string
	WhatsappSessionFile	string
	WhatsappGroup		string
	TimeToRun		string
	RunOnce			bool
	HealthchecksURL		string
}

type Album struct {
	ID			string		`json:"id"`
	Name			string		`json:"albumName"`
	Shared			bool		`json:"shared"`
	HasSharedLink		bool		`json:"hasSharedLink"`
	StartDate		time.Time	`json:"startDate"`
	CreatedAt		time.Time	`json:"createdAt"`
	AlbumThumbnailAssetId	string	`json:"albumThumbnailAssetId"`
}

type Key struct {
	ID		string		`json:"id"`
	Key		string		`json:"key"`
	Album		*Album		`json:"album"`
}

func connect(param Parameters) (*whatsmeow.Client, error) {
	dbLog := waLog.Stdout("Database", "ERROR", true)
	container, err := sqlstore.New("sqlite3", "file:" + param.WhatsappSessionFile + "?_foreign_keys=on", dbLog)
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

func testConnexions(param Parameters) error {
	// Create new WhatsApp connection and connect
	client, err := connect(param)
	if err != nil {
		return fmt.Errorf("Error connecting to WhatsApp: %v", err)
	}
	<-time.After(3 * time.Second)
	defer client.Disconnect()

	// Prints the available groups if none provided
	if param.WhatsappGroup == "" {
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
		jid, err := types.ParseJID(param.WhatsappGroup)
		if err != nil {
			return fmt.Errorf("Incorrect group identifier '%s': %v", param.WhatsappGroup, err)
		}
		_, err = client.GetGroupInfo(jid)
		if err != nil {
			return fmt.Errorf("Unknown WhatsApp group %s", param.WhatsappGroup)
		}
	}

	// Connects to Immich and load albums
	spaceClient := http.Client{
		Timeout: time.Second * 10,
	}
	req, err := http.NewRequest(http.MethodGet, param.ImmichURL + "/api/albums", nil)
	if err != nil {
		return fmt.Errorf("Error connecting to Immich with URL '%s': %v", param.ImmichURL, err)
	}
	req.Header.Set("x-api-key", param.ImmichKey)
	req.Header.Set("Accept", "application/json")

	res, err := spaceClient.Do(req)
	if err != nil {
		return fmt.Errorf("Error connecting to Immich with URL '%s': %v", param.ImmichURL, err)
	}
	if res.Body != nil {
		defer res.Body.Close()
	}
	if res.StatusCode != 200 {
		return fmt.Errorf("Error connecting to Immich with URL '%s': Status code %d", param.ImmichURL, res.StatusCode)
	}
	body, err := ioutil.ReadAll(res.Body)
	if err != nil {
		return fmt.Errorf("Error connecting to Immich with URL '%s': %v", param.ImmichURL, err)
	}
	var albums []Album
	err = json.Unmarshal([]byte(body), &albums)
	if err != nil {
		return fmt.Errorf("Error connecting to Immich with URL '%s': %v", param.ImmichURL, err)
	}

	return nil
}

func getSharingKey(album Album, param Parameters) (string, error) {
	spaceClient := http.Client{
		Timeout: time.Second * 10,
	}
	if (album.HasSharedLink) {
		// Retrieve the existing key
		req, err := http.NewRequest(http.MethodGet, param.ImmichURL + "/api/shared-links", nil)
		if err != nil {
			return "", fmt.Errorf("Error retrieving sharing key for album '%s': %v", album.Name, err)
		}
		req.Header.Set("x-api-key", param.ImmichKey)
		req.Header.Set("Accept", "application/json")
		res, err := spaceClient.Do(req)
		if err != nil {
			return "", fmt.Errorf("Error retrieving sharing key for album '%s': %v", album.Name, err)
		}
		if res.Body != nil {
			defer res.Body.Close()
		}
		if res.StatusCode != 200 {
			return "", fmt.Errorf("Error retrieving sharing key for album '%s': Status code %d", album.Name, res.StatusCode)
		}
		body, err := ioutil.ReadAll(res.Body)
		if err != nil {
			return "", fmt.Errorf("Error retrieving sharing key for album '%s': %v", album.Name, err)
		}
		var keys []Key
		err = json.Unmarshal([]byte(body), &keys)
		if err != nil {
			return "", fmt.Errorf("Error retrieving sharing key for album '%s': %v", album.Name, err)
		}
		for _, key := range keys {
			if (album.ID == key.Album.ID) {
				return key.Key, nil
			}
		}

		return "", fmt.Errorf("Error retrieving sharing key for album '%s': no key found for this albume", album.Name)
	} else {
		// Create the missing key
		var jsonData = []byte(`{
			"albumId": "` + album.ID + `",
			"allowDownload": true,
			"allowUpload": false,
			"showMetadata": true,
			"type": "ALBUM"
		}`)
		req, err := http.NewRequest(http.MethodPost, param.ImmichURL + "/api/shared-links", bytes.NewBuffer(jsonData))
		if err != nil {
			return "", fmt.Errorf("Error creating missing key for album '%s': %v", album.Name, err)
		}
		req.Header.Set("x-api-key", param.ImmichKey)
		req.Header.Set("Content-Type", "application/json")
		req.Header.Set("Accept", "application/octet-stream")
		res, err := spaceClient.Do(req)
		if err != nil {
			return "", fmt.Errorf("Error creating missing key for album '%s': %v", album.Name, err)
		}
		if res.Body != nil {
			defer res.Body.Close()
		}
		if res.StatusCode != 201 {
			return "", fmt.Errorf("Error creating missing key for album '%s': Status code %d", album.Name, res.StatusCode)
		}
		body, err := ioutil.ReadAll(res.Body)
		if err != nil {
			return "", fmt.Errorf("Error creating missing key for album '%s': %v", album.Name, err)
		}
		var key Key
		err = json.Unmarshal([]byte(body), &key)
		if err != nil {
			return "", fmt.Errorf("Error creating missing key for album '%s': %v", album.Name, err)
		}
		return key.Key, nil
	}
}

func getThumbnail(album Album, param Parameters) ([]byte, error) {
	spaceClient := http.Client{
		Timeout: time.Second * 10,
	}

	req, err := http.NewRequest(http.MethodGet, param.ImmichURL + "/api/assets/" + album.AlbumThumbnailAssetId + "/thumbnail?size=preview", nil)
	if err != nil {
		return nil, fmt.Errorf("Error retrieving thumbnail for album '%s': %v", album.Name, err)
	}
	req.Header.Set("x-api-key", param.ImmichKey)
	req.Header.Set("Accept", "application/octet-stream")
	res, err := spaceClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("Error retrieving thumbnail for album '%s': %v", album.Name, err)
	}
	if res.Body != nil {
		defer res.Body.Close()
	}
	if res.StatusCode != 200 {
		return nil, fmt.Errorf("Error retrieving thumbnail for album '%s': Status code %d", album.Name, res.StatusCode)
	}
	thumbnail, err := ioutil.ReadAll(res.Body)
	if err != nil {
		return nil, fmt.Errorf("Error retrieving thumbnail for album '%s': %v", album.Name, err)
	}

	return thumbnail, nil
}

func runLoop(param Parameters) error {
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
	req, err := http.NewRequest(http.MethodGet, param.ImmichURL + "/api/albums", nil)
	if err != nil {
		return fmt.Errorf("Error connecting to Immich with URL '%s': %v", param.ImmichURL, err)
	}
	req.Header.Set("x-api-key", param.ImmichKey)
	req.Header.Set("Accept", "application/json")

	res, err := spaceClient.Do(req)
	if err != nil {
		return fmt.Errorf("Error connecting to Immich with URL '%s': %v", param.ImmichURL, err)
	}
	if res.Body != nil {
		defer res.Body.Close()
	}
	if res.StatusCode != 200 {
		return fmt.Errorf("Error connecting to Immich with URL '%s': Status code %d", param.ImmichURL, res.StatusCode)
	}
	body, err := ioutil.ReadAll(res.Body)
	if err != nil {
		return fmt.Errorf("Error connecting to Immich with URL '%s': %v", param.ImmichURL, err)
	}
	var albums []Album
	err = json.Unmarshal([]byte(body), &albums)
	if err != nil {
		return fmt.Errorf("Error connecting to Immich with URL '%s': %v", param.ImmichURL, err)
	}

	for _, album := range albums {
		// Get albums from x years ago
		if album.Shared && (album.StartDate.Month() == time.Now().Month()) && (album.StartDate.Day() == time.Now().Day()) {
			// Retrieve the sharing key
			sharingKey, err := getSharingKey(album, param)
			if err != nil {
				return fmt.Errorf("Error retrieving the sharing key for album '%s': %v", album.Name, err)
			}

			// Retrieve the thumbnail
			thumbnail, err := getThumbnail(album, param)
			if err != nil {
				return fmt.Errorf("Error retrieving thumbnail for album '%s': %v", album.Name, err)
			}

			// Send the message
			link := param.ImmichURL + "/share/" + sharingKey
			err = sendMessage(client, param.WhatsappGroup, fmt.Sprintf("Il y a %d an(s) : %s", time.Now().Year()-album.StartDate.Year(), link), link, album.Name, thumbnail)
			if err != nil {
				fmt.Fprintf(os.Stderr, "Error sending message to WhatsApp for album '%s': %v\n", album.Name, err)
				continue
			}
		}

		// Get albums created yesterday
		if album.Shared && (album.CreatedAt.Year() == time.Now().AddDate(0, 0, -1).Year()) && (album.CreatedAt.Month() == time.Now().AddDate(0, 0, -1).Month()) && (album.CreatedAt.Day() == time.Now().AddDate(0, 0, -1).Day()) {
			// Retrieve the sharing key
			sharingKey, err := getSharingKey(album, param)
			if err != nil {
				return fmt.Errorf("Error retrieving the sharing key for album '%s': %v", album.Name, err)
			}

			// Retrieve the thumbnail
			thumbnail, err := getThumbnail(album, param)
			if err != nil {
				return fmt.Errorf("Error retrieving thumbnail for album '%s': %v", album.Name, err)
			}

			// Send the message
			link := param.ImmichURL + "/share/" + sharingKey
			err = sendMessage(client, param.WhatsappGroup, fmt.Sprintf("Nouvel album : %s", link), link, album.Name, thumbnail)
			if err != nil {
				fmt.Fprintf(os.Stderr, "Error sending message to WhatsApp for album '%s': %v\n", album.Name, err)
				continue
			}

		}
	}

	return nil
}

func parseTime(timeStr string) (int, int, error) {
	// Split the string into hours and minutes
	parts := strings.Split(timeStr, ":")
	if len(parts) != 2 {
		return 0, 0, fmt.Errorf("invalid format: %s", timeStr)
	}

	// Convert the parts to integers
	hours, err := strconv.Atoi(parts[0])
	if err != nil {
		return 0, 0, fmt.Errorf("error converting hours: %v", err)
	}

	minutes, err := strconv.Atoi(parts[1])
	if err != nil {
		return 0, 0, fmt.Errorf("error converting minutes: %v", err)
	}

	return hours, minutes, nil
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
	param := new(Parameters)
	param.ImmichURL = os.Getenv("IMMICH-URL")
	param.ImmichKey = os.Getenv("IMMICH-KEY")
	param.WhatsappSessionFile = os.Getenv("WHATSAPP-SESSION-FILE")
	param.WhatsappGroup = os.Getenv("WHATSAPP-GROUP")
	param.TimeToRun = os.Getenv("TIME-TO-RUN")
	param.HealthchecksURL = os.Getenv("HEALTHCHECKS-URL")
	_, param.RunOnce = os.LookupEnv("RUN-ONCE")

	if param.RunOnce {
		err := runLoop(*param)
		if err != nil {
			fmt.Fprintf(os.Stderr, "Error: %v\n", err)
		}
	} else {
		// Test the connexion on startup
		err := testConnexions(*param)
		if err != nil {
			fmt.Fprintf(os.Stderr, "Unable to connect: %v\n", err)
			return
		}

		// Run the loop everyday
		for {
			// First, wait for the appropriate time
			hours, minutes, err := parseTime(param.TimeToRun)
			if err != nil {
				fmt.Fprintf(os.Stderr, "Unable to read the time provided: %v\n", err)
				return
			}

			t := time.Now()
			n := time.Date(t.Year(), t.Month(), t.Day(), hours, minutes, 0, 0, t.Location())
			d := n.Sub(t)
			if d < 0 {
				n = n.Add(24 * time.Hour)
				d = n.Sub(t)
			}
			fmt.Fprintf(os.Stderr, "Sleeping for: %s\n", d)
			time.Sleep(d)

			// Then try to run it 3 times in case of error
			for i := 0; i < 3; i++ {
				err = runLoop(*param)
				if err == nil {
					continue
				}
				// Print the error and attempt number
				fmt.Fprintf(os.Stderr, "Attempt %d failed: %v\n", i+1, err)
				// Wait before the next attempt
				time.Sleep(time.Duration(math.Pow(2, float64(i))) * 30 * time.Second)
			}

			// Manage the error
			if err != nil {
				fmt.Fprintf(os.Stderr, "Error: %v\n", err)
				continue
			} else {
				if param.HealthchecksURL != "" {
					client := http.Client{
						Timeout: time.Second * 10,
					}
					_, err := client.Head(param.HealthchecksURL)
					if err != nil {
						fmt.Fprintf(os.Stderr, "%s", err)
					}
				}
			}
		}
	}
}
