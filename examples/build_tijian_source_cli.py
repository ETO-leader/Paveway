"""Build upstream's actual CLI code without linking its Desktop UI.

This is a verification binary, not an upstream release. It uses the unmodified
run_cli implementation and cli_control module plus the real upstream crates.
"""

import argparse
import json
import shutil
import subprocess
from pathlib import Path

from oicoach_runtime.integrations.tijian_catalog import UPSTREAM_REVISION


def prepare(source: Path, output: Path):
    revision = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    if revision != UPSTREAM_REVISION:
        raise ValueError("Use the documented pinned Tijian revision; review contract changes before updating")
    main_file = source / "crates/tijian-app/src/main.rs"
    code = main_file.read_text()
    # All parsing, routing, execution, output and CLI tests are upstream code.
    tail = code[code.index("async fn run_cli("):]
    control = source / "crates/tijian-app/src/cli_control.rs"
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    (output / "src/bin").mkdir(parents=True)
    lines = ['[package]', 'name = "paveway-tijian-source-probe"', 'version = "0.0.0"', 'edition = "2024"',
             '[workspace]', '[dependencies]', 'serde_json = "1"',
             'tokio = { version = "1", features = ["rt-multi-thread", "macros", "time"] }']
    for name in ("tijian-core", "tijian-command", "tijian-usecase", "tijian-control"):
        lines.append(f'{name} = {{ path = {json.dumps(str(source / "crates" / name))} }}')
    lines.extend(['[profile.dev]', 'debug = "line-tables-only"', '[profile.dev.package."*"]', 'debug = false'])
    (output / "Cargo.toml").write_text("\n".join(lines) + "\n")
    shutil.copyfile(source / "Cargo.lock", output / "Cargo.lock")
    prelude = (
        f'#[path = {json.dumps(str(control))}]\nmod cli_control;\n'
        'use std::io::Read;\n'
        'use tijian_command::{CLI_HELP_TEXT, Command, CommandData, CommandError, CommandResult, command_result_json, complete, parse_args, parse_json_args};\n'
        'use tijian_core::Database;\nuse tijian_usecase::UseCases;\n'
        'fn main() {\n'
        '    tijian_core::current_runtime().expect("invalid Tijian environment");\n'
        '    let arguments = std::env::args().skip(1).collect::<Vec<_>>();\n'
        '    if arguments.is_empty() { eprintln!("Source probe requires CLI arguments; no Desktop UI is linked"); std::process::exit(2); }\n'
        '    let runtime = tokio::runtime::Runtime::new().expect("CLI runtime");\n'
        '    std::process::exit(runtime.block_on(run_cli(arguments)));\n}\n')
    (output / "src/bin/tijian-source-cli.rs").write_text(prelude + tail)
    (output / "src/bin/tijian-source-seed.rs").write_text('''use std::sync::Arc;
use tijian_core::{Database, NewProblem, ProblemFields, ProblemStatus, RuntimeMode};
use tijian_usecase::UseCases;
fn main() {
    let environment = tijian_core::current_runtime().expect("Tijian environment");
    assert_eq!(environment.mode, RuntimeMode::Harness, "Seeder requires an explicit isolated harness profile");
    let use_cases = UseCases::new(Arc::new(Database::open_default().expect("isolated database")));
    for id in ["P1001", "P1002"] {
        use_cases.insert_problem(NewProblem { id: id.into(), fields: ProblemFields {
            name: format!("{id} source integration"), difficulty: None, quality: None,
            original_id: None, statement_md: "# Isolated test statement\\nCompute a + b.".into(),
            status: ProblemStatus::Unseen,
        }}).expect("insert through canonical UseCases");
    }
    println!("Seeded two isolated problems through upstream UseCases");
}
''')
    (output / "provenance.json").write_text(json.dumps({"upstream_revision": revision,
        "upstream_cli": str(main_file), "upstream_control": str(control),
        "desktop_linked": False, "verification_only": True}, indent=2) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True, help="New temporary build project directory")
    parser.add_argument("--cargo", default="cargo")
    args = parser.parse_args()
    try:
        prepare(args.source.resolve(), args.output.absolute())
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        parser.error(str(error))
    subprocess.run([args.cargo, "build", "--manifest-path", str(args.output.absolute() / "Cargo.toml"), "--bins"], check=True)


if __name__ == "__main__":
    main()
