"""Package project sources and an allowlisted .env for Udacity review."""
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED

from dotenv import dotenv_values


def main():
    root = Path(__file__).resolve().parents[1]
    keys = (
        'AWS_REGION', 'PROJECT_NAME', 'RETURNS_KB_ID', 'SHIPPING_KB_ID',
        'WARRANTY_KB_ID', 'AGENTCORE_RUNTIME_ARN', 'GUARDRAIL_ID',
        'GUARDRAIL_VERSION', 'ORCHESTRATOR_MODEL_ID', 'WORKER_MODEL_ID',
    )
    values = dotenv_values(root / '.env')
    missing = [key for key in keys if not values.get(key)]
    if missing:
        raise SystemExit('Missing configuration: ' + ', '.join(missing))
    output = root / 'build' / 'novamart-resubmission-draft.zip'
    output.parent.mkdir(exist_ok=True)
    files = [root / name for name in ('README.md', 'config.py', 'requirements.txt', '.env.example')]
    for folder in ('src', 'tests', 'infrastructure', 'scripts', 'submission'):
        files.extend(path for path in (root / folder).rglob('*')
                     if path.is_file() and '__pycache__' not in path.parts
                     and path.name != 'resubmission-task5.txt'  # Sandbox-only failed attempt.
                     and path.suffix in {'.py', '.yaml', '.yml', '.txt', '.png', '.jpg', '.jpeg'})
    environment = '# Model IDs are environment overrides read by config.py.\n' + ''.join(
        f'{key}={values[key]}\n' for key in keys)
    # Include a visible copy because macOS Finder hides dotfiles by default.
    (root / 'submission' / 'environment-config.txt').write_text(environment)
    files.append(root / 'submission' / 'environment-config.txt')
    with ZipFile(output, 'w', ZIP_DEFLATED) as archive:
        archive.writestr('.env', environment)
        for path in sorted(set(files)):
            archive.write(path, path.relative_to(root))
    with ZipFile(output) as archive:
        assert archive.testzip() is None
        assert '.env' in archive.namelist()
        assert archive.read('.env') == archive.read('submission/environment-config.txt')
        assert 'src/agent_orchestrator.py' in archive.namelist()
    print(f'Draft archive verified: {output}')
    print('Before submitting: verify successful tests and all required screenshots.')


if __name__ == '__main__':
    main()
