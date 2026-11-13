//! Step 4: the standalone restore tool (the release's neko_restore.py, on every card as
//! NekoBackup/tools/neko-restore.py) against what the engine writes.
//!
//! - The engine and the tool read records the same way: every fixture under
//!   tests/fixtures/cardset/records is accepted by both (valid-*) or refused by both
//!   (hostile-*). Two readers held to one set of cases, as integration report C6 says.
//! - A card the engine wrote is listed, checked and restored by the tool copied onto it,
//!   with no catalogue: A32's "from one volume with the documented tool".
//! - A damaged file is never restored (A33); a taken place is never overwritten (A34).
use super::commit;
use super::format::Record;
use super::testkit::{bytes, cands, card, put, records, run, source, Scratch, MIB};
use std::fs;
use std::os::unix::fs::MetadataExt;
use std::path::{Path, PathBuf};
use std::process::{Command, Output};

fn fixtures() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../tests/fixtures/cardset/records")
}

fn release() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("..")
}

fn python(args: &[&str]) -> Output {
    Command::new("python3")
        .args(args)
        .output()
        .expect("python3 runs the restore tool")
}

#[test]
fn the_engine_and_the_tool_judge_every_fixture_alike() {
    let mut names: Vec<PathBuf> = fs::read_dir(fixtures())
        .unwrap()
        .map(|e| e.unwrap().path())
        .collect();
    names.sort();
    assert!(names.len() >= 30, "the fixture set is there");
    // The tool's verdicts, from the same files, one line each: "<name> ok|refused".
    let script = "import sys, os\nsys.path.insert(0, sys.argv[1])\nimport neko_restore as t\n\
                  for n in sorted(os.listdir(sys.argv[2])):\n    data = open(os.path.join(sys.argv[2], n), 'rb').read()\n    \
                  try:\n        t.parse_record(data); print(n, 'ok')\n    except t.Invalid:\n        print(n, 'refused')\n";
    let out = python(&[
        "-c",
        script,
        release().to_str().unwrap(),
        fixtures().to_str().unwrap(),
    ]);
    assert!(
        out.status.success(),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
    let tool: Vec<String> = String::from_utf8(out.stdout)
        .unwrap()
        .lines()
        .map(str::to_string)
        .collect();
    let mut engine = Vec::new();
    for path in &names {
        let name = path.file_name().unwrap().to_string_lossy().into_owned();
        let verdict = if Record::parse(&fs::read(path).unwrap()).is_ok() {
            "ok"
        } else {
            "refused"
        };
        let want = if name.starts_with("valid-") {
            "ok"
        } else {
            "refused"
        };
        assert_eq!(verdict, want, "the engine on {name}");
        engine.push(format!("{name} {verdict}"));
    }
    assert_eq!(
        tool, engine,
        "the tool and the engine agree on every fixture"
    );
}

/// A card the engine wrote, with the tool on it, and the source it came from.
struct Written {
    _s: Scratch,
    card: PathBuf,
    src: super::source::Source,
    dest: PathBuf,
}

fn written(name: &str) -> Written {
    let s = Scratch::new(name);
    let src = source(&s, "photos", 1);
    put(&src, "2001/beach.jpg", &bytes(1, 70_000));
    put(&src, "2001/Beach.JPG", &bytes(2, 1000)); // the same name but for case
    put(
        &src,
        "2002/\u{05e9}\u{05dc}\u{05d5}\u{05dd} \u{1f408}.png",
        &bytes(3, 500),
    );
    put(&src, "2002/deep/er/clip.mp4", &bytes(4, 200_000));
    let (card, vol) = card(&s, 1);
    let mut cat = super::catalogue::Memory::default();
    let r = run(
        &vol,
        &mut cat,
        &card,
        4 * MIB,
        &cands(&src),
        &mut commit::no_hook,
    );
    assert_eq!(r.committed.len(), 4);
    let dest = s.dir("restored");
    Written {
        card,
        src,
        dest,
        _s: s,
    }
}

fn tool(w: &Written, args: &[&str]) -> Output {
    let path = w.card.join("NekoBackup/tools/neko-restore.py");
    let mut all = vec![path.to_str().unwrap(), "--card", w.card.to_str().unwrap()];
    all.extend_from_slice(args);
    python(&all)
}

fn text(out: &Output) -> String {
    String::from_utf8_lossy(&out.stdout).into_owned() + &String::from_utf8_lossy(&out.stderr)
}

#[test]
fn a_card_is_listed_checked_and_restored_by_the_tool_on_it() {
    // A32: nothing but the card and Python: no catalogue, no NekoServer, no network.
    let w = written("tool-restore");
    let info = tool(&w, &["info"]);
    assert!(info.status.success(), "{}", text(&info));
    assert!(text(&info).contains("Example set - 001"));
    assert!(text(&info).contains("Files:       4"));
    let verify = tool(&w, &["verify"]);
    assert!(verify.status.success(), "{}", text(&verify));
    assert!(text(&verify).contains("ok 4, damaged 0, missing 0"));
    let report = w.dest.join("report.json");
    let out = tool(
        &w,
        &[
            "restore",
            w.dest.to_str().unwrap(),
            "--rename",
            "--report",
            report.to_str().unwrap(),
        ],
    );
    assert!(out.status.success(), "{}", text(&out));
    // Each file has its bytes and its modification time back.
    for rel in [
        "2002/deep/er/clip.mp4",
        "2002/\u{05e9}\u{05dc}\u{05d5}\u{05dd} \u{1f408}.png",
    ] {
        let back = w.dest.join(rel);
        assert_eq!(
            fs::read(&back).unwrap(),
            fs::read(w.src.root.join(rel)).unwrap(),
            "{rel}"
        );
        let (a, b) = (
            fs::metadata(&back).unwrap(),
            fs::metadata(w.src.root.join(rel)).unwrap(),
        );
        assert_eq!(
            (a.mtime(), a.mtime_nsec()),
            (b.mtime(), b.mtime_nsec()),
            "{rel}"
        );
    }
    // Two names that differ only in case: one under its name, the other under
    // _renamed/, and the report says which went where.
    let report = fs::read_to_string(report).unwrap();
    assert!(report.contains("\"outcome\": \"renamed\""), "{report}");
    let renamed = fs::read_dir(w.dest.join("_renamed")).unwrap().count();
    assert_eq!(renamed, 1);
    assert_eq!(fs::read_dir(w.dest.join("2001")).unwrap().count(), 1);
    // No temporary file is left anywhere.
    assert!(!text(&out).contains(".part"));
}

#[test]
fn a_damaged_file_is_never_restored_and_a_taken_place_never_overwritten() {
    let w = written("tool-damaged");
    let recs = records(&w.card);
    let clip = recs
        .iter()
        .find(|r| r.txn().original_relative_path.ends_with("clip.mp4"))
        .unwrap();
    let payload_path = w
        .card
        .join("NekoBackup")
        .join(&clip.txn().archive_relative_path);
    let mut damaged = fs::read(&payload_path).unwrap();
    damaged[100] ^= 0xff;
    fs::write(&payload_path, &damaged).unwrap();
    // A33: verify says so; restore leaves no output of it, not even a temporary file.
    let verify = tool(&w, &["verify"]);
    assert_eq!(verify.status.code(), Some(1), "{}", text(&verify));
    assert!(text(&verify).contains("damaged  2002/deep/er/clip.mp4"));
    let out = tool(
        &w,
        &["restore", w.dest.to_str().unwrap(), "--search", "clip"],
    );
    assert_eq!(out.status.code(), Some(1), "{}", text(&out));
    let folder = w.dest.join("2002/deep/er");
    let left: Vec<_> = fs::read_dir(&folder).unwrap().flatten().collect();
    assert!(left.is_empty(), "no unverified output: {left:?}");
    // A34: a file already at the place stays as it was.
    fs::create_dir_all(w.dest.join("2001")).unwrap();
    fs::write(w.dest.join("2001/beach.jpg"), b"mine").unwrap();
    let out = tool(
        &w,
        &[
            "restore",
            w.dest.to_str().unwrap(),
            "--search",
            "2001/beach.jpg",
        ],
    );
    assert_eq!(out.status.code(), Some(1), "{}", text(&out));
    assert!(text(&out).contains("skipped"));
    assert_eq!(fs::read(w.dest.join("2001/beach.jpg")).unwrap(), b"mine");
    // Named with --overwrite, and only then, it is replaced by the checked copy.
    let out = tool(
        &w,
        &[
            "restore",
            w.dest.to_str().unwrap(),
            "--search",
            "2001/beach.jpg",
            "--overwrite",
            "2001/beach.jpg",
        ],
    );
    assert!(out.status.success(), "{}", text(&out));
    assert_eq!(
        fs::read(w.dest.join("2001/beach.jpg")).unwrap(),
        fs::read(w.src.root.join("2001/beach.jpg")).unwrap()
    );
}
