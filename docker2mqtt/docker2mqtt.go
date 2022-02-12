package main

import (
	"context"
	"flag"
	"fmt"
	"os"
	"os/signal"
	"time"
	"github.com/docker/docker/api/types"
	"github.com/docker/docker/client"
	"github.com/eclipse/paho.mqtt.golang"
)

func restartHandler(client mqtt.Client, msg mqtt.Message, dockerClient *client.Client, dockerContext context.Context) {
	fmt.Printf("Received message: %s from topic: %s\n", msg.Payload(), msg.Topic())
	var timeout = 30*time.Second
	err := dockerClient.ContainerRestart(dockerContext, string(msg.Payload()), &timeout)
	if err != nil {
		fmt.Fprintf(os.Stderr, "Unable to restart container %s: %v\n", msg.Payload(), err)
	} else {
		fmt.Fprintf(os.Stdout, "Container %s restarted\n", msg.Payload())
	}
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
	var mqttServer = flag.String("mqtt-server", "tcp://localhost:1883", "The URL of the MQTT server to connecto to")
	var mqttTopic = flag.String("mqtt-topic", "docker/events", "The MQTT topice to send the events to")
	flag.Parse()

	// Connect to the docker socket
	dockerClient, err := client.NewClientWithOpts(client.FromEnv)
	if err != nil {
		fmt.Fprintf(os.Stderr, "Unable to connect to docker: %v\n", err)
		return
	}
	dockerContext, _ := context.WithCancel(context.Background())

	// Connect to the MQTT server
	opts := mqtt.NewClientOptions().AddBroker(*mqttServer)
	mqttClient := mqtt.NewClient(opts)
	if token := mqttClient.Connect(); token.Wait() && token.Error() != nil {
		fmt.Fprintf(os.Stderr, "Unable to connect to MQTT: %v\n", token.Error())
		return
	}
	mqttClient.Subscribe(*mqttTopic + "/restart", 1, func(client mqtt.Client, msg mqtt.Message) { restartHandler(client, msg, dockerClient, dockerContext) }).Wait()

	// Listen for events
	msgs, errs := dockerClient.Events(dockerContext, types.EventsOptions{})
	for {
		select {
			case err := <- errs:
				fmt.Fprintf(os.Stderr, "Error while listening for docker events: %v\n", err)

			case msg := <-msgs:
				mqttClient.Publish(*mqttTopic + "/events", 0, false, fmt.Sprintf("{ \"time\": %d, \"type\": %q, \"name\": %q, \"action\": %q}", msg.Time, msg.Type, msg.Actor.Attributes["name"], msg.Action))
		}
	}
}
