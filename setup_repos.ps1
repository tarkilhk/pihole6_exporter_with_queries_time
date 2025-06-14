# Configuration
$GITHUB_REPO = "tarkilhk/pihole6_exporter_with_queries_time"
$GITEA_REPO = "tarkil/pihole6_exporter_with_queries_time"
$GITEA_URL = "https://gitea.hollinger.asia"

# Initialize git repository if not already done
if (-not (Test-Path .git)) {
    git init
    git add .
    git commit -m "Initial commit"
}

# Add GitHub remote
git remote add github "https://github.com/$GITHUB_REPO.git"

# Add Gitea remote
git remote add gitea "$GITEA_URL/$GITEA_REPO.git"

# Push to both remotes
git push -u github main
git push -u gitea main

Write-Host "Repository setup complete!"
Write-Host "GitHub repository: https://github.com/$GITHUB_REPO"
Write-Host "Gitea repository: $GITEA_URL/$GITEA_REPO" 