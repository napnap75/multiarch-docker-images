package immich

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io/ioutil"
	"net/http"
	"time"
)

type Album struct {
        ID                      string          `json:"id"`
        Name                    string          `json:"albumName"`
        Description             string          `json:"description"`
        Shared                  bool            `json:"shared"`
        HasSharedLink           bool            `json:"hasSharedLink"`
        StartDate               time.Time       `json:"startDate"`
        CreatedAt               time.Time       `json:"createdAt"`
        AlbumThumbnailAssetId   string  `json:"albumThumbnailAssetId"`
}

type Key struct {
        ID              string          `json:"id"`
        Key             string          `json:"key"`
        Album           *Album          `json:"album"`
}

type ImmichClient struct {
	BaseURL string
	APIKey  string
}

// Fonction pour créer une instance d'ImmichClient
func New(baseURL string, apiKey string) *ImmichClient {
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
		return nil, fmt.Errorf("Status code %d", res.StatusCode)
	}

	body, err := ioutil.ReadAll(res.Body)
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
			return "", fmt.Errorf("Status code %d", res.StatusCode)
		}

		body, err := ioutil.ReadAll(res.Body)
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
		return "", fmt.Errorf("No sharing key found for album '%s'", album.Name)
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
			return "", fmt.Errorf("Status code %d", res.StatusCode)
		}

		body, err := ioutil.ReadAll(res.Body)
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
		return nil, fmt.Errorf("Status code %d", res.StatusCode)
	}

	return ioutil.ReadAll(res.Body)
}
