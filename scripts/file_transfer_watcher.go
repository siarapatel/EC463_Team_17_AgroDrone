package main

import (
	"bufio"
	"log"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"time"

	"github.com/povsister/scp"
)

// Searches for WiFi AP and attempts to connect to it
func findAndConnect(ssid string, remotePassword string) bool {
	// First, we check for available WiFi access points
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

func main() {
	log.Println("Starting application")
	// TODO: Don't hardcode
	ssid := "pi4"
	remoteUser := "sr-design"
	remotePassword := "EC463"
	ip := "10.42.0.1"
	exportDir := filepath.Join(os.Getenv("HOME"), "export")
	ingestDir := filepath.Join("/", "home", remoteUser, "ingest")
	for {
		// should check if connected first to not spam connection attempts
		res := checkIfConnected(ssid)
		if !res {
			res = findAndConnect(ssid, "passyword")
			log.Println("Found network:", res)
			if !res {
				// if didn't find a network, sleep, then skip to the next iteration to
				// not run the transfer stuff when not connected
				time.Sleep(5 * time.Second)
				continue
			}
		}
		// now transfer files
		sshConf := scp.NewSSHConfigFromPassword(remoteUser, remotePassword)
		scpClient, err := scp.NewClient(ip, sshConf, &scp.ClientOption{})
		if err != nil {
			log.Printf("Failed to connect to server %v", err)
			time.Sleep(5 * time.Second)
			continue
		}
		log.Println("Transferring files...")
		do := &scp.DirTransferOption{
			ContentOnly: true,
		}
		err = scpClient.CopyDirToRemote(exportDir, ingestDir, do)
		if err != nil {
			log.Printf("Failed to transfer files %v", err)
			time.Sleep(5 * time.Second)
			continue
		}
		log.Println("Transfer complete")

		// And delete
		log.Println("Deleteing local files")
		entries, err := os.ReadDir(exportDir)
		if err != nil {
			log.Printf("Failed to delete %v", err)
			time.Sleep(5 * time.Second)
			continue
		}
		for _, e := range entries {
			os.RemoveAll(filepath.Join(exportDir, e.Name()))
		}

		// if files transferred, do a bigger timeout
		log.Println("Sleeping for a bit")
		time.Sleep(5 * time.Minute)
	}
}
