FROM mcr.microsoft.com/devcontainers/python:1-3.12-bookworm

# Base image includes an outdated Yarn APT signing key; feature installs use apt-get update.
# Remove the stale source before the Node and SSH devcontainer features are installed.
RUN rm -f /etc/apt/sources.list.d/yarn.list
