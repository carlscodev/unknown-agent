# Native desktop and mobile clients

The encrypted Vue client ships through three production targets from the same audited source:

- Electron desktop packages for Linux, macOS, and Windows;
- Capacitor Android application and signed App Bundle;
- Capacitor iOS application and signed App Store IPA;
- installable PWA for browsers and ChromeOS.

## Local builds

```bash
cd client
npm ci
npm run native:sync
npm run desktop:dir
```

`native:sync` builds the production web application and updates both checked-in native projects. Native tooling requires Node.js 22.12 or newer. Android requires Java 17 and the Android SDK. iOS requires macOS, Xcode, and CocoaPods.

Desktop windows run with context isolation, renderer sandboxing, Node integration disabled, web security enabled, external navigation denied, and development tools disabled in packaged builds. The application archive is ASAR-packed. The shared HTML enforces a restrictive Content Security Policy and allows API connections only over HTTPS.

Android disables cleartext traffic and mixed content. Release builds use R8 optimization and resource shrinking. Version name and code are injected by release CI. iOS uses an HTTPS application scheme and a manually selected App Store provisioning profile.

## Continuous validation

Every commit runs:

- production PWA build and Capacitor synchronization;
- Electron directory packaging on Linux, macOS, and Windows;
- Android debug assembly under Java 17;
- iOS simulator build without distribution signing;
- Python protocol and HTTP replay-protection tests.

## Signed releases

Version tags launch `.github/workflows/native-release.yml`. The workflow refuses unsigned macOS, Windows, Android, or iOS release jobs when credentials are absent.

Required repository secrets:

| Platform | Secrets |
|---|---|
| macOS | `MAC_CERTIFICATE_P12`, `MAC_CERTIFICATE_PASSWORD`, `APPLE_ID`, `APPLE_APP_SPECIFIC_PASSWORD`, `APPLE_TEAM_ID` |
| Windows | `WINDOWS_CERTIFICATE_P12`, `WINDOWS_CERTIFICATE_PASSWORD` |
| Android | `ANDROID_KEYSTORE_BASE64`, `ANDROID_STORE_PASSWORD`, `ANDROID_KEY_ALIAS`, `ANDROID_KEY_PASSWORD` |
| iOS | `IOS_DISTRIBUTION_P12`, `IOS_DISTRIBUTION_PASSWORD`, `IOS_PROVISIONING_PROFILE`, `IOS_PROFILE_NAME`, `APPLE_TEAM_ID` |
| Provenance | `UNKNOWN_AGENT_ATTEST_KEY` containing a 32-byte Ed25519 private key in hexadecimal |

The release attaches AppImage, Debian package, DMG, NSIS executable, ZIP archives, Android AAB, and iOS IPA. It also emits SHA-256 checksums, an SPDX 2.3 SBOM, and a signed DSSE/in-toto SLSA provenance envelope. macOS notarization uses Apple's notary service through electron-builder.

Generated store packages are never silently unsigned. CI jobs terminate if required signing material is absent or signature verification fails.