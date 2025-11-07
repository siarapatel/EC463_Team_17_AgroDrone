package main

import (
	"log"
	"os"
	"path/filepath"
	"time"
)

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
		err := scpDir(exportDir, ingestDir, remoteUser, remotePassword, ip)
		if err != nil {
			log.Printf("Error occured on scp: %v", err)
			time.Sleep(5 * time.Second)
			continue
		}

		// // And delete
		// log.Println("Deleteing local files")
		// entries, err := os.ReadDir(exportDir)
		// if err != nil {
		// 	log.Printf("Failed to delete %v", err)
		// 	time.Sleep(5 * time.Second)
		// 	continue
		// }
		// for _, e := range entries {
		// 	os.RemoveAll(filepath.Join(exportDir, e.Name()))
		// }

		// if files transferred, do a bigger timeout
		log.Println("Sleeping for a bit")
		time.Sleep(5 * time.Minute)
	}
}
