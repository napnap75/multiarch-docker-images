package main

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/signal"
	"regexp"
	"strings"
	"time"

	epub "github.com/go-shiori/go-epub"

	"github.com/gomarkdown/markdown"
	"github.com/gomarkdown/markdown/html"
	"github.com/gomarkdown/markdown/parser"
)

type Parameters struct {
	KitchenOwlURL       string
	KitchenOwlKey       string
	KitchenOwlHousehold string
	RecipeAuthors       string
	RecipeChapters      string
	RecipeOutput        string
}

type Household struct {
	Name  string `json:"name"`
	Photo string `json:"photo"`
}

type Item struct {
	Name        string `json:"name"`
	Description string `json:"description"`
	Icon        string `json:"icon"`
	Slug        string `json:"-"`
}

type Tag struct {
	Name string `json:"name"`
}

type Recipe struct {
	Name        string `json:"name"`
	ID          int    `json:"id"`
	Tags        []Tag  `json:"tags"`
	Description string `json:"description"`
	Items       []Item `json:"items"`
}

func Slugify(s string) string {
	// Convert to lowercase
	result := strings.ToLower(s)

	// Replace spaces with hyphens
	result = strings.ReplaceAll(result, " ", "_")

	// Return the slug
	return result
}

func MarkdownToHTML(md string) string {
	// Create markdown parser with extensions
	extensions := parser.CommonExtensions | parser.AutoHeadingIDs | parser.NoEmptyLineBeforeBlock
	p := parser.NewWithExtensions(extensions)
	doc := p.Parse([]byte(md))

	// Create HTML renderer with extensions
	htmlFlags := html.UseXHTML | html.HrefTargetBlank
	opts := html.RendererOptions{Flags: htmlFlags}
	renderer := html.NewRenderer(opts)

	return string(markdown.Render(doc, renderer))
}

func FetchHousehold(param Parameters) (*Household, error) {
	client := &http.Client{Timeout: 10 * time.Second}
	req, err := http.NewRequest(http.MethodGet, param.KitchenOwlURL+"/api/household/"+param.KitchenOwlHousehold, nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Authorization", "Bearer "+param.KitchenOwlKey)
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

	var household Household
	if err := json.Unmarshal(body, &household); err != nil {
		return nil, err
	}

	return &household, nil
}

func FetchImage(param Parameters, image string) (*string, error) {
	client := &http.Client{Timeout: 10 * time.Second}
	req, err := http.NewRequest(http.MethodGet, param.KitchenOwlURL+"/api/upload/"+image, nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Authorization", "Bearer "+param.KitchenOwlKey)
	req.Header.Set("Accept", "application/json")

	res, err := client.Do(req)
	if err != nil {
		return nil, err
	}
	defer res.Body.Close()

	if res.StatusCode != 200 {
		return nil, fmt.Errorf("status code %d", res.StatusCode)
	}

	// Open a file for writing
	filename := "/tmp/" + image
	file, err := os.Create(filename)
	if err != nil {
		return nil, err
	}
	defer file.Close()

	// Dump the response body to the file
	_, err = io.Copy(file, res.Body)
	if err != nil {
		return nil, err
	}

	return &filename, nil
}

func FetchRecipes(param Parameters) ([]Recipe, error) {
	client := &http.Client{Timeout: 10 * time.Second}
	req, err := http.NewRequest(http.MethodGet, param.KitchenOwlURL+"/api/household/"+param.KitchenOwlHousehold+"/recipe", nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Authorization", "Bearer "+param.KitchenOwlKey)
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

	var recipes []Recipe
	if err := json.Unmarshal(body, &recipes); err != nil {
		return nil, err
	}

	for recipe := range recipes {
		for item := range recipes[recipe].Items {
			recipes[recipe].Items[item].Slug = Slugify(recipes[recipe].Items[item].Name)
		}
	}

	return recipes, nil
}

// Create a new EBook with chapters and recipes
func CreateEbook(param Parameters, household Household, recipes []Recipe) (*epub.Epub, error) {
	book, err := epub.NewEpub("KitchenOwl Recipes")
	if err != nil {
		fmt.Printf("Error creating epub: %v\n", err)
	}

	// Set author
	if param.RecipeAuthors != "" {
		book.SetAuthor(param.RecipeAuthors)
	}

	// Set description
	book.SetDescription("Recipes from KitchenOwl server " + param.KitchenOwlURL)

	// Add cover image
	filename, err := FetchImage(param, household.Photo)
	if err != nil {
		fmt.Printf("Error fetching cover image: %v\n", err)
	} else {
		coverImagePath, err := book.AddImage(*filename, "cover.png")
		if err != nil {
			fmt.Printf("Error setting cover: %v\n", err)
		} else {
			err = book.SetCover(coverImagePath, "")
			if err != nil {
				fmt.Printf("Error setting cover: %v\n", err)
			}
		}
	}

	// Add an empty section at the beginning
	content := "<h1>Info</h1><ul>"
	content += "<li>Authors: " + param.RecipeAuthors + "</li>"
	content += "<li>KitchenOwl URL: " + param.KitchenOwlURL + "</li>"
	content += "<li>Household: " + household.Name + "</li>"
	content += "<li>Total Recipes: " + fmt.Sprintf("%d", len(recipes)) + "</li>"
	content += "<li>Created on: " + time.Now().Format(time.ANSIC) + "</li>"
	content += "</ul>"
	_, err = book.AddSection(content, "Info", "", "")
	if err != nil {
		fmt.Printf("Error creating section: %v\n", err)
	}

	// Process each chapter
	for _, chapter := range strings.Split(param.RecipeChapters, ",") {
		// Add a section for each specified chapter
		section, err := book.AddSection("<h1>"+chapter+"</h1>", chapter, "", "")
		if err != nil {
			fmt.Printf("Error creating section: %v\n", err)
		}

		// Add recipes that belong to the current chapter
		for _, recipe := range recipes {
			hasTag := false
			for _, tag := range recipe.Tags {
				if strings.EqualFold(tag.Name, chapter) {
					hasTag = true
					break
				}
			}
			if hasTag {
				content := "<h1>" + recipe.Name + "</h1>"

				content += "<h2>Ingrédients</h2>"
				content += "<ul>"
				for _, item := range recipe.Items {
					content += "<li>" + item.Name
					if item.Description != "" {
						content += ": " + item.Description
					}
					content += "</li>"
				}
				content += "</ul>"

				content += "<h2>Instructions</h2>"
				// Replace @item_slug with item name and description
				description := recipe.Description
				if strings.Contains(description, "@") {
					re := regexp.MustCompile(`@([^ ,]+)`)
					description = re.ReplaceAllStringFunc(description, func(match string) string {
						slug := strings.TrimPrefix(match, "@")
						for _, item := range recipe.Items {
							if item.Slug == slug {
								return item.Name + " (" + item.Description + ")"
							}
						}
						return match // return the original if not found
					})
				}
				content += MarkdownToHTML(description)

				if strings.HasPrefix(recipe.Name, "Kimchi") {
					fmt.Println(content)
				}

				_, err := book.AddSubSection(section, content, recipe.Name, "", "")
				if err != nil {
					fmt.Printf("Error adding recipe section: %v\n", err)
				}
			}
		}
	}

	return book, nil
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

	// Load parameters from the environment variables
	param := &Parameters{
		KitchenOwlURL:       os.Getenv("KITCHENOWL-URL"),
		KitchenOwlHousehold: os.Getenv("KITCHENOWL-HOUSEHOLD"),
		KitchenOwlKey:       os.Getenv("KITCHENOWL-KEY"),
		RecipeAuthors:       os.Getenv("RECIPE-AUTHORS"),
		RecipeChapters:      os.Getenv("RECIPE-CHAPTERS"),
		RecipeOutput:        os.Getenv("RECIPE-OUTPUT"),
	}

	// Fetch recipes
	fmt.Printf("Fetching recipes...")
	household, err := FetchHousehold(*param)
	if err != nil {
		fmt.Printf("Error fetching household: %v\n", err)
		return
	}
	recipes, err := FetchRecipes(*param)
	if err != nil {
		fmt.Printf("Error fetching recipes: %v\n", err)
		return
	}
	fmt.Printf(" %d recipes fetched\n", len(recipes))

	// Output the recipes to screen if specified
	if param.RecipeOutput == "" || param.RecipeOutput == "screen" {
		// Print fetched recipes
		fmt.Printf("Household: %s, Photo: %s\n", household.Name, household.Photo)
		for _, recipe := range recipes {
			fmt.Printf("Recipe: %s, ID: %d, Items: %s, Tags: %s, Description: %s\n", recipe.Name, recipe.ID, recipe.Items, recipe.Tags, recipe.Description)
		}
		return
	}

	// Create the EBook
	fmt.Printf("Creating ebook...")
	book, err := CreateEbook(*param, *household, recipes)
	if err != nil {
		fmt.Printf("Error creating ebook: %v\n", err)
		return
	}
	fmt.Println(" done")

	// Write the EBook to file if specified
	if strings.HasPrefix(param.RecipeOutput, "file:") {
		filename := strings.TrimPrefix(param.RecipeOutput, "file:")

		err := book.Write(filename)
		if err != nil {
			fmt.Printf("Error writing epub to file: %v\n", err)
		} else {
			fmt.Printf("Ebook written to %s\n", filename)
		}
	}
}
