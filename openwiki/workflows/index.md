---
type: Documentation Index
title: "Workflows"
description: "Files and subdirectories in Workflows."
---

# Files

- [Image and Repo Provisioning Workflow](image-provisioning.md) - How mprov_jobserver mirrors package repos, builds RHEL-family bare-metal images in chroots, syncs artifacts between jobservers, serves them over HTTP, and deletes images/repos.
- [Network Services - dnsmasq, NADS, and Genders](network-services.md) - How mprov_jobserver generates dnsmasq DNS/DHCP/PXE-iPXE configuration from mPCC network data, builds iPXE boot binaries, auto-discovers unknown nodes over LLDP (NADS), and renders /etc/genders for cluster tools.
- [Script Execution Workflow (script-runner)](script-execution.md) - How the mprov_jobserver script-runner module fetches, orders, and runs user scripts against system images (image-gen) or booted hosts (post-boot), the dependency graph and config merging rules, and the MProvScript SDK in script.py.
