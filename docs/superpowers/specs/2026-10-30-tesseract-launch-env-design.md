# Tesseract launch environment design

## Goal

Make the packaged Tesseract executable path available to the existing VS Code
FastAPI debug launch.

## Design

Add `TESSERACT_CMD` to the existing configuration's `env` object in
`.vscode/launch.json`, with the value
`C:\GitHub\remote-keyboard-system\server\dist\Tesseract-OCR\tesseract.exe`.
Use the VS Code JSON configuration format rather than a shell `set` command.
Do not change other launch settings or application code.

## Validation

Confirm the launch configuration remains valid JSON and that the environment
variable name and value exactly match the requested setting.
