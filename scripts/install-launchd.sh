#!/usr/bin/env bash
# install-launchd.sh
#
# Installs the EA poll launchd agent for the current user.
# Run from the project root:  bash scripts/install-launchd.sh
#
# What it does:
#   1. Fills in PROJECT_DIR and VENV_PYTHON in the plist template.
#   2. Copies the filled plist to ~/Library/LaunchAgents/.
#   3. Prompts for ANTHROPIC_API_KEY (stored in ~/Library/LaunchAgents/ plist —
#      or, if ~/.ea-env exists, uses a wrapper that sources it instead).
#   4. Loads the agent.

set -euo pipefail

LABEL="com.ea.poll"
PLIST_SRC="docs/launchd/com.ea.poll.plist"
PLIST_DST="$HOME/Library/LaunchAgents/${LABEL}.plist"
PROJECT_DIR="$(pwd)"
VENV_PYTHON="${PROJECT_DIR}/.venv/bin/python"

# ── Sanity checks ────────────────────────────────────────────────────────────

if [[ ! -f "ea.py" ]]; then
  echo "ERROR: Run this script from the ea project root (where ea.py lives)." >&2
  exit 1
fi

if [[ ! -f "${VENV_PYTHON}" ]]; then
  echo "ERROR: .venv not found. Run 'python -m venv .venv && pip install -e .' first." >&2
  exit 1
fi

if [[ ! -f "config.toml" ]]; then
  echo "ERROR: config.toml not found. Copy config.toml.example and fill it in." >&2
  exit 1
fi

if [[ ! -f "token.json" ]]; then
  echo "WARNING: token.json not found. Run 'python ea.py auth' before the agent polls."
fi

# ── API key ──────────────────────────────────────────────────────────────────

if [[ -f "$HOME/.ea-env" ]]; then
  # Safer: source from ~/.ea-env so the key isn't embedded in the plist
  echo "Found ~/.ea-env — will use wrapper script instead of embedding key in plist."
  USE_ENV_FILE=true
else
  USE_ENV_FILE=false
  if [[ -n "${ANTHROPIC_API_KEY:-}" ]]; then
    echo "Using ANTHROPIC_API_KEY from current environment."
    API_KEY="${ANTHROPIC_API_KEY}"
  else
    read -r -s -p "Enter your ANTHROPIC_API_KEY: " API_KEY
    echo
    if [[ -z "${API_KEY}" ]]; then
      echo "ERROR: API key is required." >&2
      exit 1
    fi
  fi
fi

# ── Build plist ───────────────────────────────────────────────────────────────

mkdir -p "$HOME/Library/LaunchAgents"

if [[ "${USE_ENV_FILE}" == "true" ]]; then
  # Write a tiny wrapper script that sources ~/.ea-env
  WRAPPER="${PROJECT_DIR}/scripts/ea-poll-wrapper.sh"
  cat > "${WRAPPER}" <<WRAPPER_EOF
#!/usr/bin/env bash
source "\$HOME/.ea-env"
exec "${VENV_PYTHON}" "${PROJECT_DIR}/ea.py" poll --quiet
WRAPPER_EOF
  chmod +x "${WRAPPER}"

  # Plist runs the wrapper instead of python directly
  sed \
    -e "s|CHANGEME_VENV_PYTHON|${WRAPPER}|g" \
    -e "s|CHANGEME_PROJECT_DIR/ea.py.*</string>||g" \
    -e "s|<string>poll</string>||g" \
    -e "s|<string>--quiet</string>||g" \
    -e "s|CHANGEME_PROJECT_DIR|${PROJECT_DIR}|g" \
    -e "s|CHANGEME_ANTHROPIC_API_KEY|LOADED_FROM_EA_ENV|g" \
    "${PLIST_SRC}" > "${PLIST_DST}"
else
  sed \
    -e "s|CHANGEME_VENV_PYTHON|${VENV_PYTHON}|g" \
    -e "s|CHANGEME_PROJECT_DIR|${PROJECT_DIR}|g" \
    -e "s|CHANGEME_ANTHROPIC_API_KEY|${API_KEY}|g" \
    "${PLIST_SRC}" > "${PLIST_DST}"
fi

echo "Plist written to ${PLIST_DST}"

# ── Load agent ────────────────────────────────────────────────────────────────

# Unload first in case it was previously installed
launchctl unload "${PLIST_DST}" 2>/dev/null || true
launchctl load "${PLIST_DST}"

echo ""
echo "Agent loaded. EA will poll every 5 minutes."
echo ""
echo "Useful commands:"
echo "  launchctl list ${LABEL}          # check status"
echo "  launchctl stop ${LABEL}          # stop now"
echo "  launchctl start ${LABEL}         # start now (one cycle)"
echo "  launchctl unload ${PLIST_DST}    # remove agent"
echo "  tail -f ${PROJECT_DIR}/ea.log    # watch logs"
