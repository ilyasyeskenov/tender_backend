# Railway Deployment Guide

Quick guide to deploy this tender checking service to Railway.

## Prerequisites

- Railway account (sign up at [railway.app](https://railway.app))
- This repository pushed to GitHub

## Step-by-Step Deployment

### 1. Create Railway Project

1. Go to [railway.app](https://railway.app) and sign in
2. Click **"New Project"**
3. Select **"Deploy from GitHub repo"**
4. Authorize Railway to access your GitHub
5. Select this repository (`tender_backend`)
6. Railway will create a new project and start deploying

### 2. Configure Service (Usually Auto-Detected)

Railway should automatically detect:
- **Python** runtime
- **Procfile** for start command
- **requirements.txt** for dependencies

If not auto-detected:
- **Build Command**: `pip install -r requirements.txt`
- **Start Command**: `uvicorn server:app --host 0.0.0.0 --port $PORT`

### 3. Set Environment Variables

In Railway project → **Variables** tab, add:

```
SUPABASE_URL=your_supabase_url
SUPABASE_ANON_KEY=your_supabase_anon_key
OPENAI_API_KEY=your_openai_api_key
AI_PROVIDER=openai
OPENAI_MODEL=gpt-4o
EMBEDDING_MODEL=text-embedding-3-large
```

**Optional (if using Gemini):**
```
GEMINI_API_KEY=your_gemini_key
GEMINI_MODEL=gemini-2.0-flash-exp
```

**Important:** Railway automatically sets `PORT` - don't override it.

### 4. Deploy

1. Railway will automatically deploy on push to main branch
2. Or click **"Deploy"** / **"Redeploy"** button manually
3. Watch the build logs - should see:
   ```
   Installing dependencies...
   Starting uvicorn...
   ```

### 5. Get Public URL

1. Once deployed, Railway provides a public URL automatically
2. Go to **Settings** → **Networking** → **Public Domain**
3. Copy the URL (e.g., `https://tender-backend-production.up.railway.app`)
4. Or use the default: `https://your-service-name.up.railway.app`

### 6. Update Next.js Production Environment

In your Next.js hosting (Vercel, etc.):

1. Go to **Settings** → **Environment Variables**
2. Add:
   ```
   TENDER_BACKEND_URL=https://your-tender-service.up.railway.app
   ```
3. Redeploy Next.js app

### 7. Test the Deployment

```bash
# Test health endpoint
curl https://your-tender-service.up.railway.app/health

# Should return: {"status":"ok"}
```

## Troubleshooting

### Build Fails

- Check build logs in Railway dashboard
- Ensure `requirements.txt` exists in repo root
- Verify Python version (Railway defaults to 3.11, should work)

### Service Won't Start

- Check that `Procfile` exists and uses `$PORT`
- Verify all environment variables are set
- Check logs for import errors

### Connection Refused from Next.js

- Verify `TENDER_BACKEND_URL` in Next.js env vars matches Railway URL
- Check Railway service is running (green status)
- Ensure Railway public domain is enabled

### Import Errors

If you see `ModuleNotFoundError`:

- Ensure all `__init__.py` files exist in package folders
- Check that imports use relative paths (no `tender_backend.` prefix)
- Verify the repo structure matches what's expected

## Cost

Railway free tier includes:
- $5 credit/month
- 512MB RAM
- 1GB storage
- Should be enough for moderate usage

For production scale, consider Railway Pro ($20/month) or other platforms.

## Continuous Deployment

Railway automatically deploys when you push to the main branch. Just:

1. Make changes locally
2. Commit and push to GitHub
3. Railway detects changes and redeploys automatically
