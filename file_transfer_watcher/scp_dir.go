package main

import (
	"context"
	"fmt"
	"io"
	"log"
	"os"
	"path/filepath"
	"sync/atomic"
	"time"

	scp "github.com/bramvdbogaerde/go-scp"
	"golang.org/x/crypto/ssh"
)

// scpDir copies everything inside exportDir to ingestDir on the remote host
// and shows a live transfer-speed indicator.
func scpDir(exportDir, ingestDir, remoteUser, remotePassword, ip string) error {
	// Build SSH config (password auth here)
	config := &ssh.ClientConfig{
		User: remoteUser,
		Auth: []ssh.AuthMethod{
			ssh.Password(remotePassword),
		},
		HostKeyCallback: ssh.InsecureIgnoreHostKey(),
	}

	// Create SCP client
	client := scp.NewClient(ip+":22", config)
	if err := client.Connect(); err != nil {
		return fmt.Errorf("connect: %w", err)
	}
	defer client.Close()

	// Walk local tree
	return filepath.Walk(exportDir, func(path string, info os.FileInfo, err error) error {
		if err != nil || info.IsDir() {
			return err // skip dirs, propagate real errors
		}

		relativePath, _ := filepath.Rel(exportDir, path)     // keep sub-folder structure
		remotePath := filepath.Join(ingestDir, relativePath) // remote side name

		localFile, err := os.Open(path)
		if err != nil {
			return fmt.Errorf("open local %q: %w", path, err)
		}
		// make sure to close the local file once it's done
		defer func() {
			// and check for errors
			err := localFile.Close()
			if err != nil {
				log.Printf("Error on localpath close: %v", err)
			}
		}()

		// PassThru allows you to pass in a function that gets called whenever more
		// of the file is read by the scp funciton. This allows you to add things
		// like progress tickers
		var total int64
		start := time.Now()

		passThru := func(r io.Reader, _ int64) io.Reader {
			return &speedReader{r: r, start: start, counter: &total, minDelta: time.Millisecond * 100}
		}

		// Copy with progress
		if err := client.CopyFromFilePassThru(
			context.Background(),
			*localFile,
			remotePath,
			fmt.Sprintf("%04o", info.Mode().Perm()),
			passThru,
		); err != nil {
			return fmt.Errorf("copy %q -> %q: %w", path, remotePath, err)
		}

		return nil
	})
}

// ---------- helper that prints speed on every Read ----------
type speedReader struct {
	r         io.Reader
	start     time.Time
	counter   *int64 // points to the same int64 we gave to PassThru
	lastPrint time.Time
	minDelta  time.Duration
}

func (s *speedReader) Read(p []byte) (int, error) {
	n, err := s.r.Read(p)
	atomic.AddInt64(s.counter, int64(n))

	now := time.Now()
	if now.Sub(s.lastPrint) >= s.minDelta {
		elapsed := now.Sub(s.start).Seconds()
		if elapsed > 0 {
			bps := float64(atomic.LoadInt64(s.counter)) / elapsed
			fmt.Printf("\r%s → remote  %d bytes  %.2f MiB/s",
				s.r.(*os.File).Name(),
				atomic.LoadInt64(s.counter),
				bps/1024/1024)
		}
		s.lastPrint = now
	}

	if err == io.EOF {
		fmt.Println() // final newline
	}
	return n, err
}
