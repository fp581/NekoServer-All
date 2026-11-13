# Bundled native dependencies

Cargo.lock records exact dependency versions. License/copyright files from the installed build dependencies and Rust toolchain are reproduced under licenses/. These notices apply to those dependencies and do not assign a license to NekoServer itself.

- ahash-0.8.12
- aho-corasick-1.1.5
- autocfg-1.5.1
- bitflags-2.13.2
- block-buffer-0.10.4
- cc-1.7.0
- cfg-if-1.0.5
- chrono-0.4.45
- cpufeatures-0.2.17
- crypto-common-0.1.7
- digest-0.10.7
- fallible-iterator-0.3.0
- fallible-streaming-iterator-0.1.9
- find-msvc-tools-0.1.14
- generic-array-0.14.7
- hashbrown-0.14.5
- hashlink-0.9.1
- httparse-1.10.1
- iana-time-zone-0.1.65
- itoa-1.0.18
- libc-0.2.190
- libsqlite3-sys-0.30.1
- memchr-2.8.3
- num-traits-0.2.19
- once_cell-1.21.4
- pkg-config-0.3.34
- regex-1.13.1
- regex-automata-0.4.18
- regex-syntax-0.8.11
- rusqlite-0.32.1
- serde_core-1.0.229
- serde_json-1.0.152
- sha2-0.10.9
- shlex-2.0.1
- smallvec-1.16.5
- typenum-1.20.1
- vcpkg-0.2.15
- version_check-0.9.5
- zerocopy-0.8.63
- zmij-1.0.23

libsqlite3-sys-0.30.1 builds the SQLite library from its bundled C sources (the catalogue of the removable-media backup sets). SQLite is in the public domain: https://sqlite.org/copyright.html
