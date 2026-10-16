# 🌐 OmniStream — Universal Web Video Downloader

A modern, high-performance web-based video and media downloader built with **FastAPI**, **yt-dlp**, and a glowing dark-mode glassmorphic interface. Designed to run seamlessly both locally and deployed on **Render**.

---

## ✨ Features

- 🎥 **Single Video Downloader**: Enter any public media URL (YouTube, Vimeo, Twitter/X, direct MP4/M3U8 streams, etc.).
- 🛡️ **ClassPlus & Akamai DRM (L1 + L2) Resolver**: Native automatic token signing and MPD key resolution for ClassPlus, Akamai CDN, and Testbook streams.
- 📦 **Batch / Multi-Link Downloader**: Paste multiple links (one per line) or `Title | URL` format to enqueue a playlist or batch.

- ⚙️ **Custom Video Name**: Specify your own custom file name before downloading.
- 📐 **Resolution Selector**: Easily choose between **Best (Original)**, **1080p**, **720p (HD)**, **480p (SD)**, **360p**, or **MP3 (Audio Only)**.
- 📊 **Real-time Queue Dashboard**:
  - Live percentage progress bar.
  - Transfer speed (MB/s), estimated time remaining (ETA), and file size.
  - Status badges (`Queued`, `Downloading`, `Processing`, `Completed`, `Failed`).
  - Filter tasks by status: *All*, *Active*, *Done*, *Failed*.
  - Task cancellation and clear completed tasks.
- 💾 **Direct Browser Download**: Once completed, click **Download File** to save the merged media directly to your device.
- 🚀 **100% Render Ready**: Includes `Dockerfile`, `render.yaml`, and `Procfile` configured for free/standard tier web services.

---

## 📁 Project Structure

```text
website/
├── app.py                 # FastAPI backend server with queue worker and yt-dlp
├── requirements.txt       # Python dependencies
├── Dockerfile             # Production container with ffmpeg & aria2
├── render.yaml            # Render deployment blueprint
├── Procfile               # Heroku/Render process declaration
├── downloads/             # Temporary storage for downloaded files
├── static/
│   ├── index.html         # Modern web interface
│   ├── style.css          # Glassmorphic dark design system
│   └── app.js             # Real-time queue manager & polling logic
└── README.md              # Documentation & deployment guide
```

---

## 💻 Running Locally

### 1. Prerequisites
- Python 3.10+
- `ffmpeg` installed on your system (for audio/video merging).

### 2. Setup
```bash
# Navigate to website directory
cd website

# Create a virtual environment (optional but recommended)
python -m venv venv
# On Windows:
venv\Scripts\activate
# On Linux/macOS:
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

### 3. Start the Server
```bash
uvicorn app:app --host 0.0.0.0 --port 8000 --reload
```
Open your browser and navigate to: **`http://localhost:8000`**

---

## 🚀 Deploying to Render.com

### Method 1: Deploy with Docker (Recommended)
1. Push your repository to GitHub or GitLab.
2. Log in to [Render Dashboard](https://dashboard.render.com).
3. Click **New +** -> **Web Service**.
4. Connect your Git repository.
5. In **Root Directory**, set: `website`
6. In **Environment**, choose **Docker**.
7. In **Plan**, select **Free**.
8. Click **Create Web Service**. Render will automatically build the container with Python, ffmpeg, and aria2 pre-configured!

### Method 2: Deploy with Blueprint (`render.yaml`)
1. On Render, click **Blueprints** -> **New Blueprint Instance**.
2. Select your repository.
3. Render will automatically detect `website/render.yaml` and configure the service.
