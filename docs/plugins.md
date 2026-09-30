# Signed extensions

Unknown Agent extensions are executable code. Discovery is not authorization: by
default (`[plugins] require_signed = true`) an extension is rejected before import
unless its capability manifest passes every trust check.

## Trust sequence

1. **Generate a publisher key** once:

   ```bash
   ua plugins keygen --name personal
   ```

   The private Ed25519 key is created with mode `0600` under the user config
directory. Never commit or distribute it.

2. **Trust the public key explicitly** on each machine that may run the plugin:

   ```bash
   ua plugins trust ~/.config/unknown-agent/signing/personal.pub \
     --publisher "Your Name"
   ```

3. **Create and sign a plugin**:

   ```bash
   ua plugins new project-checks
   ua plugins sign .unknown/plugins/project_checks.py \
     --key personal \
     --publisher "Your Name" \
     --capability tool:project_checks_tool:read \
     --permission tools.register \
     --permission tools.read
   ```

4. **Verify without importing code**:

   ```bash
   ua plugins verify .unknown/plugins/project_checks.py
   ua plugins list
   ```

The adjacent manifest is named `project_checks.manifest.json`. Installed
entry-point distributions use `unknown-agent-plugin.json` in distribution
metadata and bind it to the entry-point module's source file.

## What a signature covers

The canonical manifest binds all of the following to one Ed25519 signature:

- schema and runtime capability API versions;
- plugin name, version and entry point;
- publisher identity and key ID;
- SHA-256 hash of the exact executable source;
- permission grants;
- every contributed capability and each tool's risk level;
- issuance time.

Changing one byte of code or any manifest field invalidates verification.
Signatures from unknown keys are rejected even when cryptographically valid.
Publisher names must match the identity stored with the trusted key.

## Permission vocabulary

| Permission | Allows |
|---|---|
| `tools.register` | Contributing a declared tool |
| `tools.read` | A tool with `read` risk |
| `tools.write` | A tool with `write` risk |
| `tools.execute` | A tool with `execute` risk |
| `tools.network` | A tool with `network` risk |
| `tools.destructive` | A tool with `destructive` risk |
| `providers.register` | Contributing a model provider |
| `gates.register` | Contributing a validation gate |
| `hooks.register` | Subscribing a declared async hook to runtime events |
| `commands.register` | Contributing a command |
| `memory.extend` | Contributing a memory fact kind |

A tool needs both `tools.register` and the permission corresponding to its actual
runtime risk. A signed declaration of `read` cannot register a `write` tool.
Capabilities absent from the signed list are rejected.

## Atomic registration and failure containment

A verified plugin's `register()` method does not mutate the live registries
immediately. Contributions are staged in memory, checked against the manifest,
and committed only after registration finishes successfully. If the plugin
raises, requests an undeclared capability, reports a mismatched version, or
collides with an existing tool, none of its staged tools become visible.

Malformed JSON, incompatible runtime APIs, changed source, invalid signatures and
untrusted publishers are recorded by `ua plugins list`; the application continues
without importing the rejected code.

## Compatibility escape hatch

Legacy unsigned plugins can be enabled deliberately:

```toml
[plugins]
require_signed = false
```

This is a migration option, not a trust mechanism. A manifest that exists but
fails verification is always rejected; disabling the requirement cannot bypass
an invalid signature or changed content.

## Key rotation and revocation

Generate a new key, trust it, re-sign releases, then remove the old key from
`~/.config/unknown-agent/trusted_publishers.json`. Revocation prevents future
loads immediately. The trust store is local, atomic, schema-validated and written
with mode `0600`.
