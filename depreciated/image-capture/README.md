# image-capture

## Usage

To run the project, do `uv run main.py`. This inits the venv correctly so all
packages are installed for you.

## Setup

Installation:

```bash
sudo apt install -y picamera2
```

Make sure to run:

```bash
uv sync
uv venv --system-site-packages
```

`--system-site-packages` is required because `picamera2` is very tightly bound
to the hardware & local packages. So this allows the `venv` sandbox to break for
the `picamera2` package while still using the defined dependencies for
everything else.

## Internal

We are going to measure pin 27 (BCM)
