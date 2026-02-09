# Setting Up This Repository as a Standalone GitHub Repo

This guide helps you set up `tender_backend` as a separate GitHub repository for Railway deployment.

## Step 1: Create New GitHub Repository

1. Go to [GitHub](https://github.com/new)
2. Create a new repository (e.g., `tender-checker-backend`)
3. **Don't** initialize with README, .gitignore, or license (we already have these)

## Step 2: Initialize Git in This Folder

```bash
cd tender_backend
git init
git add .
git commit -m "Initial commit: Tender checker backend service"
```

## Step 3: Connect to GitHub

```bash
git remote add origin https://github.com/YOUR_USERNAME/YOUR_REPO_NAME.git
git branch -M main
git push -u origin main
```

## Step 4: Verify Structure

Your repo should have this structure:

```
tender_backend/
├── __init__.py
├── server.py              # Main FastAPI app entry point
├── requirements.txt       # Python dependencies
├── Procfile              # Railway start command
├── railway.json          # Railway configuration (optional)
├── .gitignore           # Git ignore rules
├── .env.example         # Environment variables template
├── README.md            # Main documentation
├── RAILWAY_DEPLOYMENT.md # Railway deployment guide
├── agents/              # Agent implementations
│   ├── breakdown_agent.py
│   ├── omission_checker_agent.py
│   ├── contradiction_checker_agent.py
│   └── orchestrator_agent.py
├── clients/            # Client wrappers
│   ├── ai_client.py
│   └── supabase_client.py
├── config/             # Configuration
│   └── config.py
├── prompts/            # Agent prompts
│   └── agent_prompts.py
└── tender_checker/     # Workflow
    └── workflow.py
```

## Step 5: Test Locally

Before deploying, test locally:

```bash
# Install dependencies
pip install -r requirements.txt

# Copy env example
cp .env.example .env
# Edit .env with your actual values

# Run server
python server.py
```

Visit `http://localhost:8001/health` - should return `{"status":"ok"}`

## Step 6: Deploy to Railway

See [RAILWAY_DEPLOYMENT.md](./RAILWAY_DEPLOYMENT.md) for detailed instructions.

**Quick version:**
1. Go to [railway.app](https://railway.app)
2. New Project → Deploy from GitHub
3. Select your new `tender-checker-backend` repo
4. Add environment variables (from `.env.example`)
5. Railway auto-deploys
6. Copy the public URL
7. Set `TENDER_BACKEND_URL` in your Next.js app's production env vars

## Notes

- All imports are now relative (no `tender_backend.` prefix) since this is the root repo
- The `server.py` file is the main entry point
- Railway will use `Procfile` to start the service
- Environment variables are set in Railway dashboard, not committed to git
