#!/bin/bash
# Rename mosquito/ -> engine/ and fix every import.
#
#   ./migrate.sh
#
# The folder was named when the plan was to read Nuntio's Discord feed. Most
# of what is in it — the rules, the trader, the approval gate — has nothing
# to do with Discord and is used by the Alpaca path too. Only parser.py and
# collect.py are Nuntio-specific, and they stay in case that route reopens.
#
# Safe to run twice; it checks before acting.

set -uo pipefail
cd "$(dirname "$0")" || exit 1

if [ ! -d mosquito ]; then
    echo "No mosquito/ directory — already migrated?"
    exit 0
fi

echo "Renaming mosquito/ -> engine/"
git mv mosquito engine 2>/dev/null || mv mosquito engine

if [ -f config/mosquito.yaml ]; then
    echo "Renaming config/mosquito.yaml -> config/rules.yaml"
    git mv config/mosquito.yaml config/rules.yaml 2>/dev/null \
        || mv config/mosquito.yaml config/rules.yaml
fi

echo "Rewriting imports..."
# -i '' is the macOS form; GNU sed uses -i alone.
find . -name "*.py" -not -path "./.venv/*" -not -path "./.git/*" -print0 \
    | xargs -0 sed -i '' \
        -e 's/from mosquito\./from engine./g' \
        -e 's/import mosquito\./import engine./g' \
        -e 's|config/mosquito\.yaml|config/rules.yaml|g' 2>/dev/null \
    || find . -name "*.py" -not -path "./.venv/*" -not -path "./.git/*" -print0 \
    | xargs -0 sed -i \
        -e 's/from mosquito\./from engine./g' \
        -e 's/import mosquito\./import engine./g' \
        -e 's|config/mosquito\.yaml|config/rules.yaml|g'

echo ""
echo "Checking nothing was missed:"
if grep -rn "mosquito\." --include="*.py" . 2>/dev/null | grep -v "^./engine/parser.py" | grep -v "data/mosquito"; then
    echo "  ^ review these" >&2
else
    echo "  clean"
fi

echo ""
echo "Verifying imports resolve:"
python3 -c "
import sys; sys.path.insert(0, '.')
from engine.rules import decide
from engine.approval import ApprovalQueue
from engine.paper import PaperTrader
print('  engine.rules, engine.approval, engine.paper — all import')
" || echo "  FAILED — check the errors above" >&2

echo ""
echo "Data files under data/mosquito/ were left alone so nothing already"
echo "written is orphaned. Commands to use from here:"
echo ""
echo "  python3 -m drivers.paper_live --dry-run     Alpaca feed (the live path)"
echo "  python3 -m drivers.paper_discord            Nuntio feed (if it reopens)"
