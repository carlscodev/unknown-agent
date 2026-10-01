# Sovereign platform architecture

This document complements the original coding-agent architecture with the completed four-phase platform layers.

## Durable core

Mission state, session ancestry, compaction tiers, worktrees, mutation queues, hooks, schedules, RPC sessions, gateway deliveries, media, devices, clients, benchmarks, marketplace installs, and migrations all persist independently of process lifetime. Atomic SQLite claims and bounded leases are used wherever work crosses a process boundary.

## Trust boundaries

- model output crosses typed tool and approval contracts;
- extensions cross signed capability manifests and sandbox policy;
- marketplace packages cross a pinned registry signature, digest, publisher trust, permission, and extraction boundary;
- messaging identities cross channel pairing and ACL authorization;
- devices and clients cross Ed25519 proof-of-possession plus durable nonce replay protection;
- media crosses streaming size, MIME, and SHA-256 validation;
- releases cross checksums, SPDX inventory, and signed SLSA provenance.

Secrets are references, not serialized settings. Client private keys remain non-exportable in WebCrypto stores. Durable client content is authenticated-encrypted.

## Product surfaces

The Python package provides the CLI, TUI, web control plane, gateway workers, RPC/ACP server, benchmark harness, marketplace, and bundled PWA. The client source also produces sandboxed Electron applications and Capacitor Android/iOS projects. All clients consume the same signed synchronization protocol and durable server ledger.

## Release topology

Normal CI validates Python 3.11–3.13, Windows behavior, the PWA, Electron on three desktop operating systems, Android under Java 17, and an iOS simulator build. Version tags additionally require platform signing credentials, generate store artifacts, verify signatures, calculate checksums, emit SPDX 2.3, and sign DSSE/in-toto SLSA provenance.

No runtime, lease, network request, approval, scheduler, migration lock, benchmark, or client operation is permitted to configure a timeout above 300 seconds.