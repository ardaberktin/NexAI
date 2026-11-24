# Release source packages

The monorepo produces two independent release assets. The Apple archive contains the SwiftUI apps and Xcode project. The server archive contains the Python services and deployment examples. Each includes component setup instructions, the project license, and the assistant contract.

## Package a commit

From the repository root, run this after committing the source you intend to ship:

```bash
./scripts/package-source.sh HEAD
```

Use a specific commit or release tag in place of `HEAD`. Pass an output directory as the second argument to override `dist/`. Archives draw from Git’s committed tree rather than local files. The script creates:

- `dist/nex-apple-source.zip`, with its guide at `Nex-Apple/apple/README.md`
- `dist/nex-server-source.tar.gz`, with its guide at `Nex-Server/server/README.md`

On a clean tree, build all Apple schemes, run the server suite, and unpack both archives independently. These are source downloads. Signed app releases require a developer team, provisioning, and review of the licenses for any included binaries and weights.

## Publish

Push an approved commit to your GitHub repository. Create a version tag on that commit and push the tag. Use that tag to generate the two archives, then create a draft GitHub Release and add the two files under **Assets**. Fill in the release notes and publish when ready. [GitHub release instructions](https://docs.github.com/en/repositories/releasing-projects-on-github/managing-releases-in-a-repository)

Before the first public push, review the exact commit and enable private vulnerability reporting in the GitHub repository settings. Keep model files, local archives, account secrets, and development signing files out of the committed tree.
