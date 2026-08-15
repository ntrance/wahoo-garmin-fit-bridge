# Multi-Platform & Virtual Trainer Sync Guide

This guide explains how to automatically sync `.fit` activity files from **Hammerhead Karoo**, **COROS**, **Zwift**, **MyWhoosh**, **TrainingPeaks Virtual**, and other platforms into the bridge for Garmin Connect upload and watch **Physio TrueUp** calculation.

---

## 1. How It Works (Garmin Edge Emulation Engine)

Garmin Connect normally rejects third-party activities from watch **Physio TrueUp** (Training Load, Acute Load, Recovery Time, and VO2 Max).

The bridge incorporates a **Garmin Edge Emulation Engine**:
1. **Physical Devices (Karoo, COROS, Wahoo, iGPSPORT)**: Rewrites `file_id` (Message 0) and `device_info` (Message 23) to a **Garmin Edge 1040** (`3907`) with a unique Unit ID (`3991000001`), prompting Garmin Connect to treat the ride as recorded on a genuine Garmin bike computer.
2. **Virtual Trainers (Zwift, MyWhoosh, TrainingPeaks Virtual)**: Rewrites the header to a Garmin Edge 1040 **while preserving `sub_sport: 27` (Virtual Activity)**. This displays the activity as a Virtual Ride in Garmin Connect while still feeding full TSS, VO2 Max, and Training Load down to your Garmin watch.

---

## 2. Windows PC Setup (Zwift / MyWhoosh / ROUVY / TrainingPeaks)

Virtual cycling apps automatically save completed ride `.fit` files to local folders on your computer (e.g. `Documents\Zwift\Activities`).

> [!NOTE]
> **What this command is doing:**
> The `mklink /J` command creates a **live folder link (Directory Junction)** in Windows. It connects your app's local activity folder directly into your Dropbox folder. Your files are not moved or duplicated, but Dropbox instantly sees any newly saved ride and syncs it to the bridge without you needing to drag or copy files.

### Step-by-step setup:
1. Install the official **Dropbox Desktop App** on your PC so you have a local `Dropbox` folder.
2. Open **Command Prompt** as Administrator (Search for `cmd` in the Start Menu, right-click, and select **Run as administrator**).
3. Run the command for your training application:

**Zwift:**
```cmd
mklink /J "%USERPROFILE%\Dropbox\Apps\WahooFitness\Zwift" "%USERPROFILE%\Documents\Zwift\Activities"
```

**MyWhoosh:**
```cmd
mklink /J "%USERPROFILE%\Dropbox\Apps\WahooFitness\MyWhoosh" "%USERPROFILE%\Documents\MyWhoosh"
```

**ROUVY:**
```cmd
mklink /J "%USERPROFILE%\Dropbox\Apps\WahooFitness\Rouvy" "%USERPROFILE%\Documents\ROUVY"
```

**TrainingPeaks Virtual (indieVelo):**
```cmd
mklink /J "%USERPROFILE%\Dropbox\Apps\WahooFitness\TPVirtual" "%USERPROFILE%\Documents\TrainingPeaks Virtual\Activities"
```

---

## 3. Mac / macOS Setup (Zwift / MyWhoosh)

On macOS, virtual training apps save `.fit` files to your user `Documents` folder (e.g. `~/Documents/Zwift/Activities/`).

> [!NOTE]
> **What this command is doing:**
> The `ln -s` command creates a **symbolic link (live folder alias)** between the app's local activity folder and your Dropbox sync folder. Every new ride file is automatically seen by Dropbox and uploaded by the bridge without manual file copying.

### Step-by-step setup:
1. Install the official **Dropbox Desktop App** for Mac.
2. Open the **Terminal** app (`/Applications/Utilities/Terminal.app`).
3. Run the command for your training application:

**Zwift:**
```bash
ln -s ~/Documents/Zwift/Activities ~/Dropbox/Apps/WahooFitness/Zwift
```

**MyWhoosh:**
```bash
ln -s ~/Documents/MyWhoosh ~/Dropbox/Apps/WahooFitness/MyWhoosh
```

---

## 4. Hammerhead Karoo (1, 2, 3)

The Hammerhead Karoo runs on an Android operating system and saves completed ride `.fit` files locally to `/sdcard/FitFiles/`.

### Automatic Wi-Fi Sync Setup via Dropsync:
1. Install **[Dropsync (Autosync for Dropbox) on Google Play](https://play.google.com/store/apps/details?id=com.ttxapps.dropsync&hl=en_GB)** (or sideload the APK onto your Karoo).
2. Open Dropsync and link your **Dropbox** account.
3. Configure the **Sync Folder Pair**:
   * **Local Folder**: `/sdcard/FitFiles/`
   * **Remote Folder**: `Apps/WahooFitness/Karoo` (or `Apps/WahooFitness`)
   * **Sync Method**: `Upload only`
   * **Sync Trigger**: Automatic on Wi-Fi connection.

Whenever you finish a ride outside and connect to your home Wi-Fi or phone hotspot, the Karoo `.fit` file is pushed to Dropbox and processed by the bridge.

---

## 5. COROS (DURA Head Unit & Watches)

The bridge provides a native **COROS Cloud API Source** for seamless automated syncing without needing phone file exports or third-party intermediary apps:

### 1. Automatic Cloud Sync (Recommended):
1. In the bridge web interface, navigate to **Config** and toggle on **COROS Cloud**.
2. Enter your COROS account email and password, select your account region (Americas/Global, Europe, or China), and click **Save COROS profile**.
3. Click **Test COROS login** to verify the connection.
4. **Done**: Every time you finish a ride on your COROS DURA or watch and it syncs to the COROS mobile app, the bridge automatically fetches the `.fit` file from COROS servers in the background, rewrites the device to a Garmin Edge 1040, and uploads it to Garmin Connect!

### 2. Manual 1-Tap Share from COROS App:
1. Open any completed activity in the **COROS mobile app**.
2. Tap the **Share** button in the top right corner ➔ **Export Data** ➔ **FIT**.
3. Save to your linked Dropbox folder (`Apps/WahooFitness/COROS`).

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
