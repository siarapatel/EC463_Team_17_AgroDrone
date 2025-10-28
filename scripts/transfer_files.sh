#!/bin/bash

# Copy files
scp -r $HOME/export/* pi5@10.0.0.1:/home/pi5/ingest

# Delete Directory
rm -rf $HOME/export/*
