<p align="center">
  <img src="assets/fit-to-garmin-bridge-logo.svg" width="720" alt="FIT to Garmin Bridge">
</p>

<p align="center">
  <strong>Automatically sync your rides from Wahoo, iGPSPORT, COROS, Karoo, and Virtual Cycling apps directly into Garmin Connect.</strong>
</p>

<p align="center">
  <a href="https://github.com/ntrance/wahoo-garmin-fit-bridge/actions/workflows/quality.yml"><img alt="Quality and security" src="https://github.com/ntrance/wahoo-garmin-fit-bridge/actions/workflows/quality.yml/badge.svg"></a>
  <a href="https://github.com/ntrance/wahoo-garmin-fit-bridge/releases"><img alt="Latest release" src="https://img.shields.io/github/v/release/ntrance/wahoo-garmin-fit-bridge"></a>
  <a href="LICENSE"><img alt="MIT license" src="https://img.shields.io/github/license/ntrance/wahoo-garmin-fit-bridge"></a>
</p>

<p align="center">
  <a href="https://ko-fi.com/ntr4nce">
    <img src="https://storage.ko-fi.com/cdn/kofi3.png?v=3" height="36" alt="Support this project on Ko-fi">
  </a>
</p>

---

**FIT to Garmin Bridge** is an easy-to-use, self-hosted tool designed for cyclists who record activities using non-Garmin bike computers or indoor virtual training apps, but still want their rides in **Garmin Connect** with full **Training Load**, **VO2 Max**, and **Watch Physio TrueUp** sync!

---

## 🚴 Supported Platforms

You can enable any combination of sources. The bridge runs them together in the background automatically:

| Platform / Source | Connection Type | How It Works |
| :--- | :--- | :--- |
| **Wahoo / ELEMNT** | Automatic (Dropbox) | Automatically syncs rides shared to your Dropbox from the ELEMNT app. |
| **iGPSPORT Cloud** | Automatic (Cloud API) | Automatically polls and syncs newly completed rides from your iGPSPORT account. |
| **COROS Cloud** (Watches & DURA) | Automatic (Cloud API) | Automatically polls and syncs activities from your COROS account. |
| **Hammerhead Karoo** | Automatic (Dropbox) | Syncs rides directly to Dropbox via Wi-Fi. |
| **Zwift / MyWhoosh / ROUVY / TPVirtual** | Automatic (PC/Mac Link) | Automatically detects and syncs indoor rides the moment you click "End Ride". |
| **Local Folder / NAS** | Automatic (Folder Watch) | Map any local server or network folder (`/data/incoming`) to sync `.fit` files directly. |

---

## ✨ Key Features

* **Full Garmin Watch & Physio TrueUp Sync**: Automatically rewrites activity files with a Garmin Edge 1040 profile. Garmin Connect recognizes your ride as recorded on a genuine head unit and syncs your **Training Load, Recovery Time, and VO2 Max** to your Garmin watch (**Fenix, Forerunner, Epix, Venu**).
* **Virtual Trainer Compatible**: Preserves indoor virtual ride tags for Zwift, MyWhoosh, ROUVY, and TrainingPeaks Virtual, so rides appear as virtual activities while still updating your physiological metrics.
* **Easy Web Control Panel**: Clean, modern dashboard to view your recent rides, preview routes, check live status, and configure all your platforms in one place.
* **Garmin Two-Factor Authentication (2FA/MFA) Support**: Enter your 6-digit email or SMS verification code directly in the web browser during initial setup.
* **Interactive Route Previews**: View GPS map route previews, elevation profiles, and key ride metrics instantly on the dashboard.
* **Smart Duplicate Prevention**: Advanced multi-layer deduplication ensures you never get duplicate rides uploaded to Garmin Connect.
* **Safe Dry-Run Mode**: Test your connections and preview your activity list before uploading anything to Garmin Connect.
* **Smart Background Syncing**: Automatically checks for new rides frequently during peak hours and conserves server resources overnight.
* **Lightweight & Efficient**: Runs smoothly on any system, from a Raspberry Pi or home NAS (Unraid, Synology, TrueNAS) to a VPS or cloud server.

---

## 🚀 Quick Start Guide

### Step 1: Download and Configure

Clone the repository and create your configuration file:

```bash
git clone https://github.com/ntrance/wahoo-garmin-fit-bridge.git
cd wahoo-garmin-fit-bridge
cp .env.example .env
```

Open `.env` and set a password for your web dashboard:

```dotenv
WEB_USERNAME=admin
WEB_PASSWORD=choose-a-secure-password
SESSION_SECRET_KEY=generate-a-random-secret-key
DRY_RUN=true
```

*(Tip: You can generate a random secret key with `openssl rand -hex 32`)*

### Step 2: Start the Bridge

Start the container with Docker Compose:

```bash
docker compose up -d --build
```

### Step 3: Connect Your Accounts

1. Open **`http://localhost:8088`** in your browser and log in.
2. Go to the **Config** tab.
3. Configure your desired sources:
   * **Wahoo / Dropbox**: Click *Start Dropbox Setup* and authorize your Dropbox account.
   * **iGPSPORT Cloud**: Enter your iGPSPORT login credentials.
   * **COROS Cloud**: Enter your COROS login credentials.
   * **Garmin Upload**: Enter your Garmin Connect email and password. If your account has 2FA enabled, enter the 6-digit code sent to your email when prompted.
4. Once you have verified your rides in **Dry-Run mode**, turn Dry-Run off in **Connection Settings** to begin automatic uploads!

---

## 📱 Platform Setup Instructions

### 1. Wahoo ELEMNT
1. In the **Wahoo ELEMNT app** on your phone, go to **Settings ➔ Authorized Apps ➔ Dropbox** and log in.
2. In the bridge web interface, go to **Config ➔ Dropbox** and complete the 1-click authorization.
3. Any new ride will now sync automatically from your Wahoo device ➔ Dropbox ➔ Garmin Connect!

### 2. iGPSPORT Cloud
1. In **Config ➔ iGPSPORT Cloud**, enter your iGPSPORT account email/username and password.
2. Select your account region and click **Save Profile & Test Login**.
3. Toggle on **iGPSPORT Cloud** under Connection Settings.

### 3. COROS Cloud (Watches & DURA)
1. In **Config ➔ COROS Cloud**, enter your COROS account email and password.
2. Select your account region and click **Save Profile & Test Login**.
3. Toggle on **COROS Cloud** under Connection Settings.

### 4. Hammerhead Karoo
1. Install **Dropsync** on your Karoo via sideloading ([Dropsync on Google Play](https://play.google.com/store/apps/details?id=com.ttxapps.dropsync)).
2. Configure Dropsync to automatically upload the Karoo `FitFiles` folder (`/sdcard/FitFiles`) to your Dropbox `Apps/WahooFitness` folder whenever connected to Wi-Fi.

### 5. Indoor Virtual Cycling (Zwift, MyWhoosh, ROUVY, TrainingPeaks Virtual)
Virtual apps automatically save completed `.fit` files to your computer. You can link your activity folder directly into Dropbox so rides sync automatically:

* **Windows PC**: Open Command Prompt as Administrator and run the command for your app:
  ```cmd
  REM Zwift:
  mklink /J "%USERPROFILE%\Dropbox\Apps\WahooFitness\Zwift" "%USERPROFILE%\Documents\Zwift\Activities"

  REM MyWhoosh:
  mklink /J "%USERPROFILE%\Dropbox\Apps\WahooFitness\MyWhoosh" "%USERPROFILE%\Documents\MyWhoosh"

  REM ROUVY:
  mklink /J "%USERPROFILE%\Dropbox\Apps\WahooFitness\Rouvy" "%USERPROFILE%\Documents\ROUVY"

  REM TrainingPeaks Virtual:
  mklink /J "%USERPROFILE%\Dropbox\Apps\WahooFitness\TPVirtual" "%USERPROFILE%\Documents\TrainingPeaks Virtual\Activities"
  ```
* **macOS**: Open Terminal and run:
  ```bash
  ln -s ~/Documents/Zwift/Activities ~/Dropbox/Apps/WahooFitness/Zwift
  ```

---

## 🔐 Garmin 2-Factor Authentication (2FA / MFA)

If your Garmin account has 2-Factor Authentication enabled or if you are logging in from a new cloud/VPS server, Garmin will send a 6-digit one-time code to your email or phone:

* **Web Browser**: When saving your credentials in **Config ➔ Garmin Upload**, the web interface will display a verification box. Enter your 6-digit code and click **Verify & Complete Setup**.
* **Terminal / CLI**: You can also authenticate interactively from the command line:
  ```bash
  docker compose run --rm -it bridge wahoo-bridge-garmin-login
  ```

Once verified, your session token is saved securely to `/appdata/garmin/tokens`. The bridge automatically refreshes and reuses this token in the background, so you won't need to enter 2FA codes for future uploads!

---

## 🔄 Updating to the Latest Version

The bridge automatically checks for updates and displays a notification in the header when a new version is available.

### Using Docker Compose (Source Build)
```bash
cd wahoo-garmin-fit-bridge
git pull --ff-only
docker compose up -d --build
```

### Using Pre-Built GHCR Image (`docker-compose.ghcr.yml`)
```bash
docker compose -f docker-compose.ghcr.yml pull
docker compose -f docker-compose.ghcr.yml up -d
```

---

## 📁 Persistent Data & Backup

All configuration, session tokens, and activity history are safely stored in persistent Docker volumes:

* `/appdata`: Database, settings, saved sessions, and logs.
* `/data`: Incoming, processed, and uploaded FIT files.

To back up your setup, simply back up the `./data` and `./appdata` directories.

---

## ☕ Support the Project

FIT to Garmin Bridge is open source and community-supported. If this project saves you time and improves your training workflow, consider giving it a ⭐ on GitHub or supporting ongoing development on [Ko-fi](https://ko-fi.com/ntr4nce)!

[![Support on Ko-fi](https://storage.ko-fi.com/cdn/kofi3.png?v=3)](https://ko-fi.com/ntr4nce)

---

## 📄 License & Acknowledgements

Distributed under the [MIT License](LICENSE). Built with:
* [Garmin FIT SDK](https://developer.garmin.com/fit/overview/)
* [python-garminconnect](https://github.com/cyberjunky/python-garminconnect)
* [rclone](https://rclone.org/)

> **Disclaimer**: This is an unofficial community project. It is not affiliated with, maintained, or endorsed by Wahoo Fitness, iGPSPORT, COROS, Hammerhead, Zwift, or Garmin Ltd.
