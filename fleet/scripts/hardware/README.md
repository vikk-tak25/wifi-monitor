# Hardware check for browser provisioning (#4)

Runs the same modules as the "Add unit" page (`fleet/static/provision-flow.js`, `serial-repl.js` and the
vendored esptool-js) from Node against a real board and a real server. A small adapter gives a
`node-serialport` port the Web Serial `SerialPort` shape.

```sh
cd fleet/scripts/hardware && npm install serialport@12
FLEET_PASSWORD=... node run-provision.mjs /dev/cu.usbmodem1101 https://wifi.example "Room name" [unit id] [--flash]
```

`--flash` reinstalls MicroPython even if the right version is present. The server needs the Wi-Fi
settings saved and the firmware fetched (`fleet fetch-firmware`).
