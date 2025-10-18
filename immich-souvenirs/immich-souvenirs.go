package main

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"math"
	"net/http"
	"os"
	"os/signal"
	"strconv"
	"strings"
	"time"

	_ "github.com/mattn/go-sqlite3"
	"github.com/mdp/qrterminal/v3"
	"go.mau.fi/whatsmeow"
	waProto "go.mau.fi/whatsmeow/binary/proto"
	"go.mau.fi/whatsmeow/store/sqlstore"
	"go.mau.fi/whatsmeow/types"
	waLog "go.mau.fi/whatsmeow/util/log"
	"google.golang.org/protobuf/proto"
)

type Album struct {
	ID                    string    `json:"id"`
	Name                  string    `json:"albumName"`
	Description           string    `json:"description"`
	Shared                bool      `json:"shared"`
	HasSharedLink         bool      `json:"hasSharedLink"`
	StartDate             time.Time `json:"startDate"`
	CreatedAt             time.Time `json:"createdAt"`
	AlbumThumbnailAssetId string    `json:"albumThumbnailAssetId"`
}

type Key struct {
	ID    string `json:"id"`
	Key   string `json:"key"`
	Album *Album `json:"album"`
}

type ImmichClient struct {
	BaseURL string
	APIKey  string
}

// Fonction pour créer une instance d'ImmichClient
func NewImmichClient(baseURL string, apiKey string) *ImmichClient {
	return &ImmichClient{
		BaseURL: baseURL,
		APIKey:  apiKey,
	}
}

// Récupérer la liste des albums
func (ic *ImmichClient) FetchAlbums() ([]Album, error) {
	client := &http.Client{Timeout: 10 * time.Second}
	req, err := http.NewRequest(http.MethodGet, ic.BaseURL+"/api/albums", nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("x-api-key", ic.APIKey)
	req.Header.Set("Accept", "application/json")

	res, err := client.Do(req)
	if err != nil {
		return nil, err
	}
	defer res.Body.Close()

	if res.StatusCode != 200 {
		return nil, fmt.Errorf("status code %d", res.StatusCode)
	}

	body, err := io.ReadAll(res.Body)
	if err != nil {
		return nil, err
	}

	var albums []Album
	if err := json.Unmarshal(body, &albums); err != nil {
		return nil, err
	}
	return albums, nil
}

// Obtenir la clé de partage pour un album
func (ic *ImmichClient) GetSharingKey(album Album) (string, error) {
	client := &http.Client{Timeout: 10 * time.Second}
	if album.HasSharedLink {
		// Récupérer la clé existante
		req, err := http.NewRequest(http.MethodGet, ic.BaseURL+"/api/shared-links", nil)
		if err != nil {
			return "", err
		}
		req.Header.Set("x-api-key", ic.APIKey)
		req.Header.Set("Accept", "application/json")
		res, err := client.Do(req)
		if err != nil {
			return "", err
		}
		defer res.Body.Close()

		if res.StatusCode != 200 {
			return "", fmt.Errorf("status code %d", res.StatusCode)
		}

		body, err := io.ReadAll(res.Body)
		if err != nil {
			return "", err
		}

		var keys []Key
		if err := json.Unmarshal(body, &keys); err != nil {
			return "", err
		}
		for _, key := range keys {
			if key.Album != nil && key.Album.ID == album.ID {
				return key.Key, nil
			}
		}
		return "", fmt.Errorf("no sharing key found for album '%s'", album.Name)
	} else {
		// Créer un nouveau lien partagé
		jsonData := []byte(`{
			"albumId": "` + album.ID + `",
			"allowDownload": true,
			"allowUpload": false,
			"showMetadata": true,
			"type": "ALBUM"
		}`)
		req, err := http.NewRequest(http.MethodPost, ic.BaseURL+"/api/shared-links", bytes.NewBuffer(jsonData))
		if err != nil {
			return "", err
		}
		req.Header.Set("x-api-key", ic.APIKey)
		req.Header.Set("Content-Type", "application/json")
		req.Header.Set("Accept", "application/octet-stream")
		res, err := client.Do(req)
		if err != nil {
			return "", err
		}
		defer res.Body.Close()

		if res.StatusCode != 201 {
			return "", fmt.Errorf("status code %d", res.StatusCode)
		}

		body, err := io.ReadAll(res.Body)
		if err != nil {
			return "", err
		}

		var key Key
		if err := json.Unmarshal(body, &key); err != nil {
			return "", err
		}
		return key.Key, nil
	}
}

// Récupérer la miniature d'un album
func (ic *ImmichClient) GetThumbnail(assetID string) ([]byte, error) {
	client := &http.Client{Timeout: 10 * time.Second}
	req, err := http.NewRequest(http.MethodGet, ic.BaseURL+"/api/assets/"+assetID+"/thumbnail?size=preview", nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("x-api-key", ic.APIKey)
	req.Header.Set("Accept", "application/octet-stream")
	res, err := client.Do(req)
	if err != nil {
		return nil, err
	}
	defer res.Body.Close()

	if res.StatusCode != 200 {
		return nil, fmt.Errorf("status code %d", res.StatusCode)
	}

	return io.ReadAll(res.Body)
}

type WhatsAppClient struct {
	Client *whatsmeow.Client
}

// Fonction pour initialiser la connexion et retourner une instance de WhatsAppClient
func NewWhatsAppClient(sessionFile string) (*WhatsAppClient, error) {
	dbLog := waLog.Stdout("Database", "ERROR", true)
	container, err := sqlstore.New(context.Background(), "sqlite3", "file:"+sessionFile+"?_foreign_keys=on", dbLog)
	if err != nil {
		return nil, err
	}
	deviceStore, err := container.GetFirstDevice(context.Background())
	if err != nil {
		return nil, err
	}
	clientLog := waLog.Stdout("Client", "ERROR", true)
	client := whatsmeow.NewClient(deviceStore, clientLog)

	if client.Store.ID == nil {
		// No ID stored, nouvelle connexion
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
				fmt.Println("Event:", evt.Event)
			}
		}
	} else {
		// Connexion existante
		err = client.Connect()
		if err != nil {
			return nil, err
		}
	}

	return &WhatsAppClient{Client: client}, nil
}

// Méthode pour envoyer un message
func (wac *WhatsAppClient) SendMessage(group string, title string, description string, message string, url string, thumbnail []byte) error {
	jid, err := types.ParseJID(group)
	if err != nil {
		return fmt.Errorf("incorrect group identifier '%s': %v", group, err)
	}

	msg := &waProto.Message{
		ExtendedTextMessage: &waProto.ExtendedTextMessage{
			Title:         proto.String(title),
			Description:   proto.String(description),
			Text:          proto.String(message),
			MatchedText:   proto.String(url),
			JPEGThumbnail: thumbnail,
		},
	}
	ts, err := wac.Client.SendMessage(context.Background(), jid, msg)
	if err != nil {
		return fmt.Errorf("error sending message with title '%s': %v", title, err)
	}
	fmt.Printf("Message with title '%s' sent (timestamp: %s)\n", title, ts)
	return nil
}

type Parameters struct {
	ImmichURL           string
	ImmichKey           string
	WhatsappSessionFile string
	WhatsappGroup       string
	TimeToRun           string
	DevelopmentMode     string
	HealthchecksURL     string
}

func parseTime(timeStr string) (int, int, error) {
	parts := strings.Split(timeStr, ":")
	if len(parts) != 2 {
		return 0, 0, fmt.Errorf("invalid format: %s", timeStr)
	}
	hours, err := strconv.Atoi(parts[0])
	if err != nil {
		return 0, 0, err
	}
	minutes, err := strconv.Atoi(parts[1])
	if err != nil {
		return 0, 0, err
	}
	return hours, minutes, nil
}

func main() {
	// Capture interrupt signal for graceful shutdown
	c := make(chan os.Signal, 1)
	signal.Notify(c, os.Interrupt)
	go func() {
		<-c
		fmt.Println("Got interrupt signal. Exiting...")
		os.Exit(0)
	}()

	// Load parameters from environment variables
	param := &Parameters{
		ImmichURL:           os.Getenv("IMMICH-URL"),
		ImmichKey:           os.Getenv("IMMICH-KEY"),
		WhatsappSessionFile: os.Getenv("WHATSAPP-SESSION-FILE"),
		WhatsappGroup:       os.Getenv("WHATSAPP-GROUP"),
		TimeToRun:           os.Getenv("TIME-TO-RUN"),
		DevelopmentMode:     os.Getenv("DEVELOPMENT-MODE"),
		HealthchecksURL:     os.Getenv("HEALTHCHECKS-URL"),
	}

	// Initialize WhatsApp connection
	wac, err := NewWhatsAppClient(param.WhatsappSessionFile)
	if err != nil {
		fmt.Fprintf(os.Stderr, "error initializing WhatsApp: %v\n", err)
		return
	}

	// Initialize Immich client
	immichClient := NewImmichClient(param.ImmichURL, param.ImmichKey)

	switch param.DevelopmentMode {
	case "run-once", "run-last":
		if err := runLoop(wac, immichClient, param); err != nil {
			fmt.Fprintf(os.Stderr, "error in runLoop: %v\n", err)
		}
	case "listen":
		if err := listen(wac); err != nil {
			fmt.Fprintf(os.Stderr, "error in listen: %v\n", err)
		}
	default:
		// Test connection on startup
		if err := testConnections(wac, immichClient, param); err != nil {
			fmt.Fprintf(os.Stderr, "connection test failed: %v\n", err)
			return
		}
		// Main scheduled loop
		for {
			hours, minutes, err := parseTime(param.TimeToRun)
			if err != nil {
				fmt.Fprintf(os.Stderr, "invalid time format: %v\n", err)
				return
			}
			now := time.Now()
			target := time.Date(now.Year(), now.Month(), now.Day(), hours, minutes, 0, 0, now.Location())
			if target.Before(now) {
				target = target.Add(24 * time.Hour)
			}
			sleepDuration := target.Sub(now)
			fmt.Printf("Sleeping for: %v\n", sleepDuration)
			time.Sleep(sleepDuration)

			// Retry logic
			for i := 0; i < 3; i++ {
				err := runLoop(wac, immichClient, param)
				if err == nil {
					break
				}
				fmt.Fprintf(os.Stderr, "attempt %d failed: %v\n", i+1, err)
				time.Sleep(time.Duration(math.Pow(2, float64(i))) * 30 * time.Second)
			}
			if err != nil {
				fmt.Fprintf(os.Stderr, "error after retries: %v\n", err)
			} else if param.HealthchecksURL != "" {
				client := &http.Client{Timeout: 10 * time.Second}
				_, err := client.Head(param.HealthchecksURL)
				if err != nil {
					fmt.Fprintf(os.Stderr, "healthcheck error: %v\n", err)
				}
			}
		}
	}
}

// Fonction pour tester la connexion à WhatsApp et Immich
func testConnections(wac *WhatsAppClient, immichClient *ImmichClient, param *Parameters) error {
	fmt.Println("Testing connections...")
	// Test WhatsApp
	if err := wac.Client.Connect(); err != nil {
		return fmt.Errorf("whatsApp connection failed: %v", err)
	}
	defer wac.Client.Disconnect()
	fmt.Println("WhatsApp connected.")

	// Test Immich
	albums, err := immichClient.FetchAlbums()
	if err != nil {
		return fmt.Errorf("immich connection failed: %v", err)
	}
	fmt.Printf("fetched %d albums from Immich.\n", len(albums))
	return nil
}

// Fonction principale pour exécuter la logique
func runLoop(wac *WhatsAppClient, immichClient *ImmichClient, param *Parameters) error {
	// Connecter WhatsApp si nécessaire
	if !wac.Client.IsConnected() {
		if err := wac.Client.Connect(); err != nil {
			return err
		}
	}
	defer wac.Client.Disconnect()

	// Charger albums depuis Immich
	albums, err := immichClient.FetchAlbums()
	if err != nil {
		return err
	}

	for _, album := range albums {
		if album.Shared {
			// Récupérer les albums anniversaire
			if param.DevelopmentMode == "run-last" || (album.StartDate.Month() == time.Now().Month() && album.StartDate.Day() == time.Now().Day()) {
				// Obtenir la clé de partage
				sharingKey, err := immichClient.GetSharingKey(album)
				if err != nil {
					fmt.Fprintf(os.Stderr, "erreur récupération clé partage: %v\n", err)
					continue
				}
				link := param.ImmichURL + "/share/" + sharingKey

				// Récupérer la miniature
				thumbnail, err := immichClient.GetThumbnail(album.AlbumThumbnailAssetId)
				if err != nil {
					fmt.Fprintf(os.Stderr, "erreur miniature: %v\n", err)
					continue
				}

				// Envoyer le message
				err = wac.SendMessage(param.WhatsappGroup, album.Name, album.Description, fmt.Sprintf("Il y a %d an(s) : %s", time.Now().Year()-album.StartDate.Year(), link), link, thumbnail)
				if err != nil {
					fmt.Fprintf(os.Stderr, "erreur envoi WhatsApp: %v\n", err)
				}
			}

			if param.DevelopmentMode == "run-last" {
				return nil
			}

			// Récupérer les albums de la veille
			if album.CreatedAt.Year() == time.Now().AddDate(0, 0, -1).Year() && (album.CreatedAt.Month() == time.Now().AddDate(0, 0, -1).Month()) && (album.CreatedAt.Day() == time.Now().AddDate(0, 0, -1).Day()) {
				// Obtenir la clé de partage
				sharingKey, err := immichClient.GetSharingKey(album)
				if err != nil {
					fmt.Fprintf(os.Stderr, "erreur récupération clé partage: %v\n", err)
					continue
				}
				link := param.ImmichURL + "/share/" + sharingKey

				// Récupérer la miniature
				thumbnail, err := immichClient.GetThumbnail(album.AlbumThumbnailAssetId)
				if err != nil {
					fmt.Fprintf(os.Stderr, "erreur miniature: %v\n", err)
					continue
				}

				// Envoyer le message
				err = wac.SendMessage(param.WhatsappGroup, album.Name, album.Description, fmt.Sprintf("Nouvel album : %s", link), link, thumbnail)
				if err != nil {
					fmt.Fprintf(os.Stderr, "erreur envoi WhatsApp: %v\n", err)
				}
			}
		}
	}
	return nil
}

// Fonction pour écouter les événements WhatsApp
func listen(wac *WhatsAppClient) error {
	if err := wac.Client.Connect(); err != nil {
		return err
	}
	defer wac.Client.Disconnect()

	wac.Client.AddEventHandler(func(evt interface{}) {
		fmt.Println("réception d'un message:", evt)
	})

	// Attendre une interruption
	c := make(chan os.Signal, 1)
	signal.Notify(c, os.Interrupt)
	<-c
	fmt.Println("arrêt demandé.")
	return nil
}
