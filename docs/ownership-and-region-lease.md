# Ownership and region leases

Every planned child has a declared write region. An active ChildTask has at most one active OwnerAssignment. Transfer requires explicit release. Unknown overlap semantics fail closed.

Local ChangeSets are rescanned from the Owner workspace. Each changed path must resolve to exactly one active same-revision file or directory region before integration. Database and external-object observations now have a pluggable semantic resolver contract; the default remains fail-closed and accepts nothing without an exact active lease match.

Writer Leases support acquisition, release, heartbeat, expiry, and overlap checks. The local adapter enforces file and directory changes. Database/external adapters must implement the resolver protocol and pass resource identity, selector, and base-revision checks; production provider/database adapters are still outside v0.0.1.
