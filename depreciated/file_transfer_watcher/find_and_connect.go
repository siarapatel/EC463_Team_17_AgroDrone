package main

import (
	"bufio"
	"log"
	"os/exec"
	"strings"
)

// Searches for WiFi AP and attempts to connect to it
func findAndConnect(ssid string, remotePassword string) bool {
	// First, we check for available WiFi access points
	_ = exec.Command("nmcli", "dev", "wifi", "rescan").Run()
	cmd := exec.Command("nmcli", "-f", "SSID,SIGNAL,SECURITY", "dev", "wifi")
	out, err := cmd.Output()
	if err != nil {
		log.Printf("scan failed: %v", err)
		return false
	}

	scanner := bufio.NewScanner(strings.NewReader(string(out)))
	for scanner.Scan() {
		line := scanner.Text()
		// now check if it contains the pi4 ssid
		if strings.Contains(line, ssid) && strings.Contains(line, "WPA2") {
			// Now if it does have the ssid, check if the SSID is already locally
			// registered
			args := []string{"con", "show", ssid}
			log.Printf("executing command: nmcli %s", strings.Join(args, " "))
			cmd := exec.Command("nmcli", args...)
			_, err := cmd.Output()
			if err != nil {
				// if it doesn't yet exist, create a new connection
				args = []string{"device", "wifi", "connect", ssid}
				if remotePassword != "" {
					args = append(args, "remotePassword", remotePassword)
				}
				log.Printf("executing command: nmcli %s", strings.Join(args, " "))
				cmd = exec.Command("nmcli", args...)
				_, err = cmd.Output()
				if err != nil {
					log.Printf("connection failed: %v", err)
					return false
				}
			} else {
				// if it does exist, just connect to it
				args = []string{"device", "wifi", "connect", ssid}
				log.Printf("executing command: nmcli %s", strings.Join(args, " "))
				cmd = exec.Command("nmcli", args...)
				_, err = cmd.Output()
				if err != nil {
					log.Printf("connection failed: %v", err)
					return false
				}
			}
			return true
		}
	}
	return false
}

func checkIfConnected(_ string) bool { return false }
