package main

import (
	"fmt"
	"math"
	"net/http"
	"os"
	"os/signal"
	"strconv"
	"strings"
	"time"

	"github.com/napnap75/multiarch-docker-files/immich-souvenirs/internals/immich"
	"github.com/napnap75/multiarch-docker-files/immich-souvenirs/internals/whatsapp"
)

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
	wac, err := whatsapp.New(param.WhatsappSessionFile)
	if err != nil {
		fmt.Fprintf(os.Stderr, "Error initializing WhatsApp: %v\n", err)
		return
	}

	// Initialize Immich client
	immichClient := immich.New(param.ImmichURL, param.ImmichKey)

	switch param.DevelopmentMode {
	case "run-once", "run-last":
		if err := runLoop(wac, immichClient, param); err != nil {
			fmt.Fprintf(os.Stderr, "Error in runLoop: %v\n", err)
		}
	case "listen":
		if err := listen(wac); err != nil {
			fmt.Fprintf(os.Stderr, "Error in listen: %v\n", err)
		}
	default:
		// Test connection on startup
		if err := testConnections(wac, immichClient, param); err != nil {
			fmt.Fprintf(os.Stderr, "Connection test failed: %v\n", err)
			return
		}
		// Main scheduled loop
		for {
			hours, minutes, err := parseTime(param.TimeToRun)
			if err != nil {
				fmt.Fprintf(os.Stderr, "Invalid time format: %v\n", err)
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
				fmt.Fprintf(os.Stderr, "Attempt %d failed: %v\n", i+1, err)
				time.Sleep(time.Duration(math.Pow(2, float64(i))) * 30 * time.Second)
			}
			if err != nil {
				fmt.Fprintf(os.Stderr, "Error after retries: %v\n", err)
			} else if param.HealthchecksURL != "" {
				client := &http.Client{Timeout: 10 * time.Second}
				_, err := client.Head(param.HealthchecksURL)
				if err != nil {
					fmt.Fprintf(os.Stderr, "Healthcheck error: %v\n", err)
				}
			}
		}
	}
}

// Fonction pour tester la connexion à WhatsApp et Immich
func testConnections(wac *whatsapp.WhatsAppClient, immichClient *immich.ImmichClient, param *Parameters) error {
	fmt.Println("Testing connections...")
	// Test WhatsApp
	if err := wac.Client.Connect(); err != nil {
		return fmt.Errorf("WhatsApp connection failed: %v", err)
	}
	defer wac.Client.Disconnect()
	fmt.Println("WhatsApp connected.")

	// Test Immich
	albums, err := immichClient.FetchAlbums()
	if err != nil {
		return fmt.Errorf("Immich connection failed: %v", err)
	}
	fmt.Printf("Fetched %d albums from Immich.\n", len(albums))
	return nil
}

// Fonction principale pour exécuter la logique
func runLoop(wac *whatsapp.WhatsAppClient, immichClient *immich.ImmichClient, param *Parameters) error {
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
					fmt.Fprintf(os.Stderr, "Erreur récupération clé partage: %v\n", err)
					continue
				}
				link := param.ImmichURL + "/share/" + sharingKey

				// Récupérer la miniature
				thumbnail, err := immichClient.GetThumbnail(album.AlbumThumbnailAssetId)
				if err != nil {
					fmt.Fprintf(os.Stderr, "Erreur miniature: %v\n", err)
					continue
				}

				// Envoyer le message
				err = wac.SendMessage(param.WhatsappGroup, album.Name, album.Description, fmt.Sprintf("Il y a %d an(s) : %s", time.Now().Year()-album.StartDate.Year(), link), link, thumbnail)
				if err != nil {
					fmt.Fprintf(os.Stderr, "Erreur envoi WhatsApp: %v\n", err)
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
					fmt.Fprintf(os.Stderr, "Erreur récupération clé partage: %v\n", err)
					continue
				}
				link := param.ImmichURL + "/share/" + sharingKey

				// Récupérer la miniature
				thumbnail, err := immichClient.GetThumbnail(album.AlbumThumbnailAssetId)
				if err != nil {
					fmt.Fprintf(os.Stderr, "Erreur miniature: %v\n", err)
					continue
				}

				// Envoyer le message
				err = wac.SendMessage(param.WhatsappGroup, album.Name, album.Description, fmt.Sprintf("Nouvel album : %s", link), link, thumbnail)
				if err != nil {
					fmt.Fprintf(os.Stderr, "Erreur envoi WhatsApp: %v\n", err)
				}
			}
		}
	}
	return nil
}

// Fonction pour écouter les événements WhatsApp
func listen(wac *whatsapp.WhatsAppClient) error {
	if err := wac.Client.Connect(); err != nil {
		return err
	}
	defer wac.Client.Disconnect()

	wac.Client.AddEventHandler(func(evt interface{}) {
		fmt.Println("Réception d'un message:", evt)
	})

	// Attendre une interruption
	c := make(chan os.Signal, 1)
	signal.Notify(c, os.Interrupt)
	<-c
	fmt.Println("Arrêt demandé.")
	return nil
}
