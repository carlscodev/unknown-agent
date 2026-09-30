# Secure marketplace

Unknown Agent's marketplace never imports package code during discovery or verification. Trust is established in layers before a plugin reaches an executable plugin directory.

## Trust chain

1. A registry index is Pydantic-validated and verified with a pinned Ed25519 registry key.
2. Indexes are valid for at most 24 hours; future, expired, malformed, or oversized indexes fail closed.
3. The selected non-yanked release must not use a publisher key on the signed revocation list.
4. The downloaded ZIP is limited to 50 MB and matched against the signed SHA-256 digest.
5. Extraction rejects absolute paths, traversal, nested layouts, more than 1,000 entries, and expansion beyond 50 MB.
6. The package must contain exactly one plugin source and one signed capability manifest.
7. The existing publisher trust store verifies publisher identity, Ed25519 signature, runtime API compatibility, source digest, declared permissions, capabilities, and risk levels.
8. Package name, version, and publisher key must exactly match the signed registry release.

Only after every check succeeds are source and manifest atomically promoted. Failed upgrades restore the previous pair. Marketplace state is written through a mode-0600 atomic replacement.

## Operations

```bash
ua marketplace install example-plugin \
  --index https://market.example/index.json \
  --registry-key "$PINNED_REGISTRY_PUBLIC_KEY"

ua marketplace list --registry-key "$PINNED_REGISTRY_PUBLIC_KEY"
ua marketplace uninstall example-plugin --registry-key "$PINNED_REGISTRY_PUBLIC_KEY"
```

The registry key is raw Ed25519 public-key bytes encoded as base64. Publisher keys are managed with the existing `ua plugins trust` and `ua plugins revoke` commands.

The installer selects the highest available semantic version by default. Yanked releases are excluded. Downgrades require `--allow-downgrade`; upgrades never silently expand permissions because the newly signed capability manifest is revalidated in full. Network operations use HTTPS, reject redirects by default, and enforce a timeout no greater than 300 seconds.