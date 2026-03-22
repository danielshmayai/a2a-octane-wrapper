#!/usr/bin/env bash
echo "Configuring repository to use .githooks as git hooks path..."
git config core.hooksPath .githooks
echo "Done. To remove: git config --unset core.hooksPath" 
