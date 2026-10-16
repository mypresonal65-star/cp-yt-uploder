# 🌐 OmniStream — High-Speed Web Video Downloader & YouTube Auto-Uploader

A powerful, high-performance web-based video downloader and YouTube auto-uploader built with **FastAPI**, **yt-dlp**, and a modern glassmorphic dark-mode UI. Features native **ClassPlus & Akamai DRM (L1 + L2)** resolution, **16x parallel fragment downloading**, multi-user **YouTube OAuth2 uploader** via Cloudflare D1, and **1-hour automatic disk cleanup**.

---

## ✨ Key Features

- ⚡ **High-Speed Parallel Engine**: Downloads HLS/DASH streams with **16 concurrent fragments** and real Chrome browser client-hints to eliminate CDN throttling (stable 5–8+ MB/s).
- 📺 **Multi-User YouTube Auto-Upload**: Authenticate your own YouTube channel with `client_secrets.json` via Cloudflare Worker & D1 database. Downloaded videos upload automatically to YouTube as Unlisted/Private/Public.
- 🛡️ **ClassPlus & Akamai DRM Resolver**: Automatic token signing, key resolution, and decryption for ClassPlus, Akamai CDN, and Testbook streams.
- 🧹 **Automatic & 1-Click Disk Cleanup**:
  - Automatically deletes video files from storage **1 hour after YouTube upload** (or download) finishes.
  - Retains task history and **Watch on YouTube** direct links forever.
  - Header **`💾 Disk: ... MB 🧹`** button frees disk space instantly in 1 click.
- 🔄 **Real-Time Live Queue & Refresh Button**:
  - Live progress bar, fragment tracker (`Frag X/Y`), speed, and ETA.
  - Dedicated **"🔄 Refresh"** button to update queue instantly without page reloads.
  - Smart in-place DOM updates (zero screen flickers or scroll jumps).
- 📦 **Batch Downloader**: Paste multiple video links or `Title | URL` format to enqueue entire batches easily.

---

## 🚀 Heroku Deployment Guide (Easy Step-by-Step)

Deploy your own 24/7 web instance on Heroku in 4 simple steps:

### Step 1: Push Code to GitHub
In your local computer terminal, run:
```bash
git add .
git commit -m "Deploy to Heroku"
git push
```

### Step 2: Create a New Heroku App
1. Log in to [Heroku Dashboard](https://dashboard.heroku.com).
2. Click **New** (top right) -> **Create new app**.
3. Enter any unique **App name** (e.g. `my-cp-yt-downloader`) and click **Create app**.

### Step 3: Add Buildpacks & Config Var (Settings Tab)
1. Go to the **Settings** tab.
2. Scroll down to **Buildpacks** and click **Add buildpack**:
   - Paste this FFmpeg URL and click **Save changes**:
     ```text
     https://github.com/jonathanong/heroku-buildpack-ffmpeg-latest.git
     ```
3. Click **Add buildpack** again, select **python**, and click **Save changes**.  
   *(Make sure FFmpeg is #1 and Python is #2 in the list).*
4. In the **Config Vars** section above, click **Reveal Config Vars**:
   - **KEY:** `CLOUDFLARE_AUTH_API`
   - **VALUE:** `https://yt-uplode.mypresonal65.workers.dev`
   - Click **Add**.

### Step 4: Connect GitHub & Deploy
1. Go to the **Deploy** tab.
2. Under **Deployment method**, click the **GitHub** icon and connect your GitHub account.
3. Search for your repository (`cp-yt-uploder`) and click **Connect**.
4. Scroll down to **Manual deploy**, select branch `main`, and click **Deploy Branch**.
5. Once build completes (takes ~2 minutes), click **Open app** to launch your live downloader!

---

## ⚡ Google Colab Setup (Zero-Cost On-Demand)

Run completely free on Google Colab with high-speed Google cloud network:

### Cell 1: Clone & Setup Dependencies
```python
!git clone https://github.com/mypresonal65-star/cp-yt-uploder
%cd cp-yt-uploder
!pip install -r requirements.txt
!apt-get install -y ffmpeg aria2 > /dev/null 2>&1
```

### Cell 2: Install LocalTunnel (or Ngrok)
```python
!npm install -g localtunnel
```

### Cell 3: Start Server & Expose Public URL
```python
import subprocess
import time
import urllib.request

# Start FastAPI server in background
subprocess.Popen(["python3", "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"], cwd="website")
time.sleep(3)

# Print LocalTunnel IP (password for localtunnel page)
ip = urllib.request.urlopen('https://ipv4.icanhazip.com').read().decode('utf8').strip()
print(f"🔑 LocalTunnel Password / Endpoint IP: {ip}")

# Expose port 8000
!npx localtunnel --port 8000
```
Open the generated `.loca.lt` URL, paste the IP password, and enjoy high-speed downloads!

---

## 🌐 Deploying to Render.com

1. In [Render Dashboard](https://dashboard.render.com), click **New +** -> **Web Service**.
2. Connect your GitHub repository.
3. Set **Root Directory** to `website`.
4. Set **Environment** to `Docker` (uses `website/Dockerfile` with FFmpeg, aria2, and Bento4 mp4decrypt pre-installed).
5. Choose **Free** plan and click **Create Web Service**.

---

## 💻 Running Locally

### Prerequisites
- Python 3.10+
- [FFmpeg](https://ffmpeg.org/download.html) installed and added to your system PATH.

### Installation
```bash
# Clone the repository
git clone https://github.com/mypresonal65-star/cp-yt-uploder.git
cd cp-yt-uploder

# Install dependencies
pip install -r requirements.txt

# Start local server
cd website
python -m uvicorn app:app --host 0.0.0.0 --port 8000 --reload
```
Open browser at: **`http://localhost:8000`**
