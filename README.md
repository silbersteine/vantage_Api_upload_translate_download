# Translate files with Vantage

Send one or more files to a Lokalise Vantage project and save the translations on your machine.

You need Python 3.11 or newer, a read/write Lokalise API token, and the UUID of a Vantage file project. In Lokalise, open your avatar, then Profile settings, then API tokens, and create a read/write token.

Put your token and project UUID in `.env` in this folder. That file stays on your machine. The command finds it from this folder or from a subfolder such as `source-lang`.

```powershell
py -3 -m pip install -e .
py -3 -m vantage_client --input source-lang --output translated
```

The `.` on the install line is the current folder. Leave it in.

`--input` can be a file or a folder. Repeat it to send more than one path. A folder is searched for DOCX, PPTX, XLSX, HTML, Markdown, and IDML files. Each file must be 50 MB or smaller.

The command reads the project’s target languages, uploads the files, waits for the AI translation, and writes the results to `translated`. If the project also uses human review, it downloads the AI files once they are ready and does not wait for reviewers. A file named `guide.docx` comes back as `guide.de-DE.docx` for German, and the same pattern for each other target language. Only the files from this run are downloaded.

To limit the languages:

```powershell
py -3 -m vantage_client --input source-lang\getting-started.md --locales de-DE,fr --output translated
```

Without `--locales`, every target language on the project is included. The default wait for each step is 30 minutes.

GitHub Actions runs when you start the workflow by hand. On `master`, it sends only the `source-lang` files that changed in the latest commit. On any other branch, it sends only the `source-lang` files that differ from `master`. If nothing changed, it stops before calling Vantage. It then opens a branch named `translations/` plus the triggering branch, the GitHub run id, and the attempt number, commits `translated/` there, and opens a pull request back to the triggering branch. Each run gets its own branch and pull request. The Lokalise token is a repository secret, `VANTAGE_API_TOKEN`. The project id is a repository variable, `VANTAGE_PROJECT_ID`.
