<img src="src/assets/logo.png" width="128" height="128" style="border-radius: 100%;">

# Birdcam Frontend

This is the frontend vite/react application for viewing birdcam livestream and managing your clipped recordings.

Right now it just runs as dev. The frontend proxies API and stream requests to the Flask backend on localhost:5000 during development. See the root README for the backend setup. Looking to make a better path for making the latest front-end available via GH releases, or something, soon. Be sure to update `allowedHosts` within `vite.config.js` if you wish to make this accessible publicly or over your local network. 

### Ensure you have Node / NPM

```bash
node --version # should be greater than 20.1*.*
npm --version # should be greater than 9.*.*
```

If not, figure that out, then:

```bash
npm install
npm run dev
```

Open `http://<hostname>:5173` in your browser. You should see the live stream and (if any motion is detected) recorded clips.

## Systemd Service

If you want the front-end to persist like the camera/server, first ensure the server service is setup (see the root README), then:

```bash
cp birdcam-frontend.service /etc/systemd/system/birdcam-frontend.service
sudo systemctl daemon-reload
sudo systemctl enable birdcam-frontend.service
sudo systemctl start birdcam-frontend.service
sudo systemctl status birdcam-frontend.service
journalctl -fu birdcam-frontend.service # follow logs
```

And yes it still runs dev for now.

Happy frontending!
