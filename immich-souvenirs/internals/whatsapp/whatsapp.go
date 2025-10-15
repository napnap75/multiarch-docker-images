package whatsapp

import (
	"context"
	"fmt"
	"os"

        _ "github.com/mattn/go-sqlite3"
	"github.com/mdp/qrterminal/v3"
	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/store/sqlstore"
	waLog "go.mau.fi/whatsmeow/util/log"
	"go.mau.fi/whatsmeow/types"
	waProto "go.mau.fi/whatsmeow/binary/proto"
	"google.golang.org/protobuf/proto"
)

type WhatsAppClient struct {
	Client *whatsmeow.Client
}

// Fonction pour initialiser la connexion et retourner une instance de WhatsAppClient
func New(sessionFile string) (*WhatsAppClient, error) {
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
		return fmt.Errorf("Incorrect group identifier '%s': %v", group, err)
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
		return fmt.Errorf("Error sending message with title '%s': %v", title, err)
	}
	fmt.Printf("Message with title '%s' sent (timestamp: %s)\n", title, ts)
	return nil
}
