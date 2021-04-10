package main

import (
	"context"
	"flag"
	"fmt"
	"os"
	"os/signal"
	"github.com/docker/docker/api/types"
	"github.com/docker/docker/client"
	"github.com/eclipse/paho.mqtt.golang"
)

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
	var mqttServer = flag.String("mqtt-server", "tcp://localhost:1883", "The URL of the MQTT server to connecto to")
	var mqttTopic = flag.String("mqtt-topic", "docker/events", "The MQTT topice to send the events to")
	flag.Parse()

	// Connect to the docker socket
	cli, err := client.NewClientWithOpts(client.FromEnv)
	if err != nil {
		fmt.Fprintf(os.Stderr, "Unable to connect to docker: %v\n", err)
		return
	}
	ctx, _ := context.WithCancel(context.Background())

	// Connect to the MQTT server
        opts := mqtt.NewClientOptions().AddBroker(*mqttServer)
        client := mqtt.NewClient(opts)
        if token := client.Connect(); token.Wait() && token.Error() != nil {
		fmt.Fprintf(os.Stderr, "Unable to connect to docker: %v\n", token.Error())
		return
        }

	// Listen for events
	msgs, errs := cli.Events(ctx, types.EventsOptions{})
	for {
		select {
			case err := <- errs:
				fmt.Fprintf(os.Stderr, "Error while listening for docker events: %v\n", err)

			case msg := <-msgs:
				client.Publish(*mqttTopic, 0, false, fmt.Sprintf("{ \"time\": %d, \"type\": \"%s\", \"name\": \"%s\", \"action\": \"%s\"}\n", msg.Time, msg.Type, msg.Actor.Attributes["name"], msg.Action))
		}
	}
}
