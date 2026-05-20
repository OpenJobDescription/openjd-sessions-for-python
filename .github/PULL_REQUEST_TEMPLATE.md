Fixes: *<insert link to GitHub issue here>*

### What was the problem/requirement? (What/Why)

### What was the solution? (How)

### What is the impact of this change?

### How was this change tested?

See [DEVELOPMENT.md](https://github.com/OpenJobDescription/openjd-model-for-python/blob/mainline/DEVELOPMENT.md#testing) for information on running tests.

- Have you run the unit tests?

### Was this change documented?

- Are relevant docstrings in the code base updated?

### Is this a breaking change?

A breaking change is one that modifies a public contract in a way that is not backwards compatible. See the
[Public Interfaces](https://github.com/OpenJobDescription/openjd-model-for-python/blob/mainline/DEVELOPMENT.md#the-packages-public-interface) section
of the DEVELOPMENT.md for more information on the public contracts.

If so, then please describe the changes that users of this package must make to update their scripts, or Python applications.

### Does this change impact security?

- Does the change need to be threat modeled? For example, does it create or modify files/directories that must only be readable by the process owner?
    - If so, then please label this pull request with the "security" label. We'll work with you to analyze the threats.

### Cross-port to openjd-rs

This package is being migrated to Rust in [`openjd-rs/crates/openjd-sessions`](https://github.com/OpenJobDescription/openjd-rs/tree/main/crates/openjd-sessions).
Behavioral changes made here should be replicated there to keep the two
implementations in sync until the migration is complete.

- [ ] This change does not affect runtime behavior (docs / tests / tooling only), **or**
- [ ] A matching change has been opened in `openjd-rs` (link the PR here): *<insert link>*, **or**
- [ ] A tracking issue has been filed in `openjd-rs` to port this change (link here): *<insert link>*, **or**
- [ ] Cross-porting is not applicable for this change because: *<reason>*

----

*By submitting this pull request, I confirm that you can use, modify, copy, and redistribute this contribution, under the terms of your choice.*