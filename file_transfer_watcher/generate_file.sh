#!/bin/bash

# 10 x 10MB files is realistic for an export of photos
for i in {1..10}; do
  dd if=/dev/urandom of="$HOME/export/testfile_$i" bs=1M count=10
done
