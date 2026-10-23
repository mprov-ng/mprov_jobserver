---
type: Documentation Index
title: "Architecture"
description: "Files and subdirectories in Architecture."
---

# Files

- [Job Server Architecture Overview](overview.md) - How the mprov_jobserver daemon boots, loads YAML config with !include, authenticates to the mProv Control Center over an API-key session, registers with heartbeat, and drives job-module plugin threads and job status updates.
- [Job Module Plugin Framework](plugin-framework.md) - How mprov_jobserver job modules are structured as JobServerPlugin threads, discovered from the plugins package, configured from merged YAML, and supervised by the JobServer heartbeat loop, plus how to add a new plugin.
