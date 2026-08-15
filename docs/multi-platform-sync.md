# Multi-Platform & Virtual Trainer Sync Guide

This guide explains how to automatically sync `.fit` activity files from **Hammerhead Karoo**, **COROS**, **Zwift**, **MyWhoosh**, **TrainingPeaks Virtual**, and other platforms into the bridge for Garmin Connect upload and watch **Physio TrueUp** calculation.

---

## 1. How It Works (Garmin Edge Emulation Engine)

Garmin Connect normally rejects third-party activities from watch **Physio TrueUp** (Training Load, Acute Load, Recovery Time, and VO2 Max).

The bridge incorporates a **Garmin Edge Emulation Engine**:
1. **Physical Devices (Karoo, COROS, Wahoo, iGPSPORT)**: Rewrites `file_id` (Message 0) and `device_info` (Message 23) to a **Garmin Edge 1040** (`3907`) with a unique Unit ID (`3991000001`), prompting Garmin Connect to treat the ride as recorded on a genuine Garmin bike computer.
2. **Virtual Trainers (Zwift, MyWhoosh, TrainingPeaks Virtual)**: Rewrites the header to a Garmin Edge 1040 **while preserving `sub_sport: 27` (Virtual Activity)**. This displays the activity as a Virtual Ride in Garmin Connect while still feeding full TSS, VO2 Max, and Training Load down to your Garmin watch.

---

## 2. Windows PC Setup (Zwift / MyWhoosh / Rouvy)

On Windows, virtual training apps save `.fit` files locally:
* **Zwift**: `%USERPROFILE%\Documents\Zwift\Activities\`
* **MyWhoosh**: `%USERPROFILE%\Documents\MyWhoosh\`

### Automatic Sync Setup (Windows Directory Junction):
1. Install the official **Dropbox Desktop App** on your PC.
2. Ensure your Dropbox sync folder contains `Apps\WahooFitness` (or create it).
3. Open **Command Prompt** as Administrator (`cmd.exe`).
4. Run this command to link your Zwift activities directly to Dropbox:

```cmd
mklink /J "%USERPROFILE%\Dropbox\Apps\WahooFitness\Zwift" "%USERPROFILE%\Documents\Zwift\Activities"
```

*(For MyWhoosh, replace the target path with your MyWhoosh activities directory).*

> [!TIP]
> Whenever you finish an indoor ride in Zwift, Windows immediately mirrors the `.fit` file into Dropbox. The bridge detects it, converts the file to a Garmin Edge 1040 virtual ride, and uploads it to Garmin Connect within seconds.

---

## 3. Mac / macOS Setup (Zwift / MyWhoosh)

On macOS, Zwift and MyWhoosh save `.fit` files to:
* `~/Documents/Zwift/Activities/`

### Automatic Sync Setup (macOS Symlink):
1. Install the official **Dropbox Desktop App** on your Mac.
2. Open the **Terminal** app (`/Applications/Utilities/Terminal.app`).
3. Run the following command:

```bash
ln -s ~/Documents/Zwift/Activities ~/Dropbox/Apps/WahooFitness/Zwift
```

> [!TIP]
> Any ride completed on your Mac will sync directly to Dropbox -> Bridge -> Garmin Connect with zero manual export required.

---

## 4. Hammerhead Karoo (1, 2, 3)

The Hammerhead Karoo runs on an Android operating system and saves `.fit` files locally to `/sdcard/FitFiles/`.

### Automatic Wi-Fi Sync Setup:
1. Enable Developer Options / Sideloading on your Karoo.
2. Install an Android background folder sync app (such as **FolderSync** or **Autosync for Dropbox**).
3. Configure the sync pair:
   * **Local Folder**: `/sdcard/FitFiles/`
   * **Remote Folder**: `Dropbox / Apps / WahooFitness / Karoo`
   * **Sync Trigger**: Automatic on Wi-Fi connection.

Whenever you finish a ride outside and connect to your home Wi-Fi or phone hotspot, the Karoo `.fit` file is pushed to Dropbox and processed by the bridge.

---

## 5. COROS (DURA Head Unit & Watches)

* **Via COROS Training Hub**: Export the `.fit` file directly into your linked Dropbox folder (`Apps/WahooFitness/COROS`).
* **Via Automatic Cloud Sync**: Link your COROS account to auto-export to Dropbox or a watched local directory.

---

## 6. Direct Local Folder Watcher (`/data/incoming`)

If you run the bridge on a home server or NAS (Unraid, Synology, TrueNAS) and prefer not to use Dropbox:

1. Map a local network folder on your host machine to `/data/incoming` inside the Docker container in `docker-compose.yml`:
   ```yaml
   volumes:
     - /path/to/my/local/fit_folder:/data/incoming
     - ./data:/data
     - ./appdata:/appdata
   ```
2. Drop any `.fit` file from any device or platform into that folder.
3. The bridge incoming watcher will detect the file, run the Garmin Edge 1040 emulation, and upload it to Garmin Connect automatically.
