#!/usr/bin/env bash
#
# bootstrap.sh - Infect a new repository with the Autonomous Agent Harness
#
# Usage:
#   curl -sL <url>/bootstrap.sh | bash
#   # or
#   ./path/to/bootstrap.sh
#

set -euo pipefail

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

log_info() { echo -e "${BLUE}[INFO]${NC} $1"; }
log_success() { echo -e "${GREEN}[OK]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }

# Determine the source directory (where this script lives)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HARNESS_ROOT="$(dirname "$SCRIPT_DIR")"
TARGET_DIR="${1:-.}"

echo ""
echo "=========================================="
echo "  Autonomous Agent Harness - Bootstrap"
echo "=========================================="
echo ""

# Validate target directory
if [[ ! -d "$TARGET_DIR" ]]; then
    log_error "Target directory does not exist: $TARGET_DIR"
    exit 1
fi

cd "$TARGET_DIR"
TARGET_DIR="$(pwd)"
log_info "Target directory: $TARGET_DIR"

# Check if already initialized
if [[ -f ".claude/CLAUDE.md" ]]; then
    log_warn "Agent harness already exists in this directory."
    read -p "Overwrite existing configuration? (y/N) " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        log_info "Aborted."
        exit 0
    fi
fi

# Copy harness files
log_info "Copying harness files..."

# Create directories
mkdir -p .claude harness/templates specs tests/e2e

# Copy .claude directory
cp -r "$HARNESS_ROOT/.claude/"* .claude/ 2>/dev/null || true
log_success "Copied .claude/"

# Copy harness directory (excluding bootstrap.sh itself to avoid confusion)
cp "$HARNESS_ROOT/harness/loop.py" harness/
cp -r "$HARNESS_ROOT/harness/templates/"* harness/templates/ 2>/dev/null || true
log_success "Copied harness/"

# Copy specs template if specs is empty
if [[ ! -f "specs/features.json" ]]; then
    cp "$HARNESS_ROOT/harness/templates/features.json" specs/features.json
    log_success "Initialized specs/features.json"
else
    log_warn "specs/features.json already exists, skipping"
fi

# Initialize git if not already
if [[ ! -d ".git" ]]; then
    log_info "Initializing git repository..."
    git init
    log_success "Git initialized"
fi

# Create/update .gitignore
log_info "Updating .gitignore..."
touch .gitignore

declare -a IGNORES=(
    "node_modules/"
    ".env"
    ".env.local"
    "*.log"
    "playwright-report/"
    "test-results/"
    ".playwright/"
    "__pycache__/"
    "*.pyc"
    ".venv/"
    "venv/"
)

for pattern in "${IGNORES[@]}"; do
    if ! grep -qxF "$pattern" .gitignore 2>/dev/null; then
        echo "$pattern" >> .gitignore
    fi
done
log_success "Updated .gitignore"

# Check for package.json, create if missing
if [[ ! -f "package.json" ]]; then
    log_info "Creating package.json..."
    cat > package.json << 'EOF'
{
  "name": "autonomous-agent-project",
  "version": "0.1.0",
  "private": true,
  "scripts": {
    "test": "playwright test",
    "test:ui": "playwright test --ui",
    "test:headed": "playwright test --headed",
    "agent:loop": "python3 harness/loop.py"
  },
  "devDependencies": {}
}
EOF
    log_success "Created package.json"
fi

# Install dependencies
log_info "Installing Playwright..."
npm install --save-dev @playwright/test
npx playwright install --with-deps chromium
log_success "Playwright installed"

log_info "Installing MCP servers..."
npm install --save-dev @anthropic-ai/mcp-server-playwright @anthropic-ai/mcp-server-filesystem @anthropic-ai/mcp-server-git 2>/dev/null || {
    log_warn "Some MCP servers failed to install (they may not be published yet)"
}

# Create Playwright config if missing
if [[ ! -f "playwright.config.ts" ]]; then
    log_info "Creating Playwright configuration..."
    cat > playwright.config.ts << 'EOF'
import { defineConfig, devices } from '@playwright/test';

export default defineConfig({
  testDir: './tests/e2e',
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  workers: process.env.CI ? 1 : undefined,
  reporter: 'html',
  use: {
    baseURL: process.env.BASE_URL || 'http://localhost:3000',
    trace: 'on-first-retry',
    screenshot: 'only-on-failure',
  },
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],
  webServer: process.env.BASE_URL ? undefined : {
    command: 'npm run dev',
    url: 'http://localhost:3000',
    reuseExistingServer: !process.env.CI,
    timeout: 120000,
  },
});
EOF
    log_success "Created playwright.config.ts"
fi

# Create empty placeholder files
touch specs/.gitkeep tests/e2e/.gitkeep

echo ""
echo "=========================================="
log_success "Agent Harness installed successfully!"
echo "=========================================="
echo ""
echo "Next steps:"
echo "  1. Edit specs/features.json to define your features"
echo "  2. Run: python3 harness/loop.py"
echo ""
echo "Files created:"
echo "  .claude/CLAUDE.md      - Agent constitution"
echo "  .claude/config.json    - MCP server configuration"
echo "  harness/loop.py        - The autonomous loop script"
echo "  specs/features.json    - Your feature backlog"
echo "  playwright.config.ts   - Playwright configuration"
echo ""
