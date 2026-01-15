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
mkdir -p .claude harness/coding harness/templates harness/reviewers specs specs/memory specs/context tests/e2e

# Copy .claude directory
cp -r "$HARNESS_ROOT/.claude/"* .claude/ 2>/dev/null || true
log_success "Copied .claude/"

# Copy architect (specification harness)
cp "$HARNESS_ROOT/harness/architect.py" harness/
cp "$HARNESS_ROOT/harness/archaeologist.py" harness/
cp "$HARNESS_ROOT/harness/doctor.py" harness/
cp "$HARNESS_ROOT/harness/review_board.py" harness/
cp "$HARNESS_ROOT/harness/memory.py" harness/
log_success "Copied harness/architect.py"
log_success "Copied harness/archaeologist.py"
log_success "Copied harness/doctor.py"
log_success "Copied harness/review_board.py"
log_success "Copied harness/memory.py"

# Copy reviewers module (Bicameral Mind)
cp -r "$HARNESS_ROOT/harness/reviewers/"* harness/reviewers/ 2>/dev/null || true
log_success "Copied harness/reviewers/"

# Copy coding harness (execution loop)
cp "$HARNESS_ROOT/harness/coding/loop.py" harness/coding/
cp "$HARNESS_ROOT/harness/coding/mcp_manager.py" harness/coding/
cp "$HARNESS_ROOT/harness/coding/verify.py" harness/coding/
cp "$HARNESS_ROOT/harness/coding/git_utils.py" harness/coding/
cp "$HARNESS_ROOT/harness/coding/repo_map.py" harness/coding/
cp "$HARNESS_ROOT/harness/coding/reflection.py" harness/coding/
cp "$HARNESS_ROOT/harness/coding/review.py" harness/coding/
cp -r "$HARNESS_ROOT/harness/templates/"* harness/templates/ 2>/dev/null || true
log_success "Copied harness/coding/"

# Copy specs templates
if [[ ! -f "specs/features.json" ]]; then
    cp "$HARNESS_ROOT/harness/templates/features.json" specs/features.json
    log_success "Initialized specs/features.json"
else
    log_warn "specs/features.json already exists, skipping"
fi

if [[ ! -f "specs/learnings.json" ]]; then
    cp "$HARNESS_ROOT/specs/learnings.json" specs/learnings.json
    log_success "Initialized specs/learnings.json"
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
    "doctor": "python3 harness/doctor.py",
    "doctor:diagnose": "python3 harness/doctor.py diagnose",
    "doctor:stabilize": "python3 harness/doctor.py stabilize",
    "doctor:baseline": "python3 harness/doctor.py baseline",
    "doctor:fixtures": "python3 harness/doctor.py fixtures",
    "doctor:qa": "python3 harness/doctor.py qa",
    "doctor:solidify": "python3 harness/doctor.py solidify",
    "spec": "python3 harness/architect.py",
    "spec:new": "python3 harness/architect.py new",
    "spec:resume": "python3 harness/architect.py resume",
    "spec:audit": "python3 harness/architect.py audit",
    "agent:loop": "python3 harness/coding/loop.py"
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

# Note: No Python dependencies needed - harness uses Claude CLI for all LLM calls
log_success "Dependencies installed"

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
echo "  1. Ensure Claude CLI is installed: npm install -g @anthropic-ai/claude-code"
echo "  2. Run health check (brownfield): python3 harness/doctor.py"
echo "  3. Specify your product: python3 harness/architect.py new \"Your idea\""
echo "  4. Run the coding loop: python3 harness/coding/loop.py"
echo ""
echo "Files created:"
echo ""
echo "  Phase 0 (Health Check):"
echo "    harness/doctor.py         - Brownfield health audit & stabilization"
echo ""
echo "  Specification Harness:"
echo "    harness/architect.py      - Socratic specification REPL"
echo "    harness/archaeologist.py  - Pattern extraction for brownfield"
echo "    harness/memory.py         - Working memory persistence"
echo ""
echo "  Bicameral Review Board:"
echo "    harness/review_board.py   - Cross-model adversarial review"
echo "    harness/reviewers/        - Pluggable review providers"
echo ""
echo "  Coding Harness:"
echo "    harness/coding/loop.py         - Main orchestration loop"
echo "    harness/coding/mcp_manager.py  - MCP server lifecycle"
echo "    harness/coding/verify.py       - External test verification"
echo "    harness/coding/git_utils.py    - Git checkpoint/rollback"
echo "    harness/coding/repo_map.py     - Codebase structure mapping"
echo "    harness/coding/reflection.py   - Knowledge transfer"
echo "    harness/coding/review.py       - Pattern enforcement"
echo ""
echo "  Configuration:"
echo "    .claude/CLAUDE.md       - Agent constitution"
echo "    .claude/config.json     - MCP server configuration"
echo "    specs/features.json     - Feature backlog (shared contract)"
echo "    specs/learnings.json    - Accumulated lessons"
echo "    playwright.config.ts    - Playwright configuration"
echo ""
echo "Key features:"
echo "  - Brownfield Doctor: Health audit before building on existing code"
echo "  - Five Gates: Adversarial specification before any code"
echo "  - Bicameral Review: Cross-model adversarial review at critical stages"
echo "  - Working Memory: Q&A persistence survives context summarization"
echo "  - External verification: Harness runs tests, not the agent"
echo "  - Git checkpoints: Commit on green, rollback on red"
echo "  - Regression fence: All tests must pass before feature completes"
echo "  - Reflection: Lessons learned persist for future agents"
echo "  - Pattern enforcement: Extracts and enforces codebase conventions"
echo "  - Webhook fixtures: Scaffold for testing external integrations"
echo ""
