# Theara

Theara is a local PySide6 mail-code app that reads recent verification codes from Yandex, Zoho, Outlook, and Hotmail inboxes without marking messages as read.

The top workspace switch has three pages:

- **Yandex Mail** opens first and shows Yandex plus existing password-based/Zoho mailboxes.
- **Hotmail / Outlook** shows Microsoft mailboxes and uses the existing secure Microsoft OAuth sign-in flow. Password or third-party refresh-token lists are not imported.
- **Get 2FA Code** generates the current six-digit, 30-second authenticator code from a Base32 secret. The secret field is shown by default, including after **Clear**, and can still be hidden manually. Generation is local; the secret is held only in memory and is never saved or sent.

It can also permanently delete all mail from explicitly selected Yandex
mailboxes after typed confirmation, while preserving their folder structure.

The default search window is **last 1 hour**. After fetching, the upper table summarizes the latest matching message and status for each account, while the lower **All Mail** table lists every Inbox message in the selected time window across all fetched accounts. Its main columns show the actual recipient/Yandex submail, the Facebook name detected from Facebook's greeting, and the mail code. Click a value in **CODE MAIL** to copy it and show a short confirmation popup. **Codes first** is enabled by default so verification messages stay above ordinary notifications while each group remains newest-first; clear it to restore one global newest-first list. Double-click either an upper account row or an exact All Mail row to open the decoded message body.

Khmer Facebook names and message bodies use the bundled **Noto Sans Khmer** font, so no separate font installation is required on another Windows device. The included font is distributed under the SIL Open Font License in `assets/fonts/OFL-NotoSansKhmer.txt`.

Drag any column header to resize it, or use **Columns -**, **Fit columns**, and **Columns +** (`Ctrl+-`, `Ctrl+0`, `Ctrl++`) for both tables. The upper search box filters both tables. The dedicated **Find** box beside **All Mail** filters that list by Facebook name or Yandex submail using case-insensitive partial matching; press `Ctrl+F` to focus it. On the 2FA page, `Ctrl+F` focuses the secret field. Both mail filters are applied together when both contain text.

## Run

Double-click `Start Theara.bat`. The launcher installs missing requirements automatically. You can also run:

```powershell
python -m pip install -r requirements.txt
python main.py
```

Writable accounts, settings, and Microsoft authorization data are stored under
`%LOCALAPPDATA%\Theara`. On the first source launch, an older adjacent data file
is copied there without deleting or overwriting anything.

## Build for Windows

Double-click `Build Theara.bat`, or run:

```powershell
powershell -ExecutionPolicy Bypass -File ".\Build Theara.ps1"
```

The build script installs the pinned build tools, regenerates the icon, runs the
test suite, creates the windowed PyInstaller bundle, smoke-tests `Theara.exe`,
and produces:

```text
dist\Theara\Theara.exe
dist\Theara-windows-x64.zip
```

Distribute the complete `dist\Theara` folder or the ZIP. The build explicitly
checks that account, settings, and token-cache files were not included.

## Add accounts

For Yandex and standard Zoho addresses, paste one account per line:

```text
name@yandex.com|mail-app-password
name@zoho.com|zoho-password-or-app-password
```

The pasted-list format contains login credentials only. To add a Yandex recipient alias after importing, select its row and click **Edit alias**.

For a Zoho custom-domain mailbox, either choose its server preset in **Add**, or prefix a pasted line with `zoho` (the prefix uses the global personal server):

```text
zoho|name@company.com|zoho-app-password
```

### Yandex

The app uses `imap.yandex.com:993` with SSL/TLS. Enable mail-client IMAP access and use a Yandex Mail app password.

#### Prefer a Yandex plus alias

Each Yandex row can optionally use a plus-address such as `name+432teamC@yandex.com`:

1. Add or import the base login mailbox, such as `name@yandex.com`.
2. Select that row and click **Edit alias**. You can also enter the alias while using **Add**.
3. Enter an alias with the same base mailbox and domain plus a non-empty `+tag`.

When an alias is set, the app still signs in with the base mailbox. The alias is a preference for the latest-code cell in the upper table: an exact, case-insensitive recipient-header match wins when available. The All Mail table still shows every recent message and its actual dynamic submail. If the configured alias is stale and has no recent code, the app falls back to the newest Inbox code instead of returning an empty result. It checks `To`, `Cc`, `Delivered-To`, `X-Original-To`, `Envelope-To`, `X-Envelope-To`, `Apparently-To`, and `Resent-To`; text that merely appears in the subject or body is not treated as an alias match.

To stop preferring an alias, select the row, click **Edit alias**, clear the field, and confirm. A blank alias always uses the newest recent verification message in that mailbox. Aliases are saved inside the same Windows-encrypted account store.

#### Permanently delete all Yandex mail

Select one or more visible Yandex account rows, then click **Delete all Yandex mail**. Hidden rows and non-Yandex rows are excluded. The action does not use the checked rows (which are selected by default for fetching), and it requires typing the exact confirmation `DELETE ALL MAIL` before anything changes.

The app permanently removes all messages present in every real, selectable
Yandex folder, including Inbox, Subscriptions, Social, Sent, Drafts/templates,
Outbox, Archive, Spam, Trash, and custom folders. It preserves the folder
structure, Yandex account, and account settings. **With attachments** is an
aggregate view and clears when its backing messages are deleted. A configured
plus-alias is ignored, so the action affects the entire mailbox; messages
arriving after a folder's snapshot may remain.

This action is irreversible. Trash is included and emptied, so the deleted messages cannot be recovered.

- [Yandex mail-client setup](https://yandex.com/support/yandex-360/customers/mail/en/mail-clients/others)
- [Yandex folder behavior](https://yandex.com/support/yandex-360/customers/mail/en/web/mailbox/folders)
- [Yandex message deletion and recovery](https://yandex.com/support/yandex-360/customers/mail/en/web/letter/delete)
- [Yandex encryption settings](https://yandex.com/support/yandex-360/customers/mail/en/mail-clients/ssl)

### Zoho Mail

Enable IMAP under Zoho Mail settings first. With 2FA, use a Zoho app-specific password. Zoho's correct hostname depends on the account type and data center; select the server shown under **Mail Accounts → Primary Account → Server Configuration Details**. The UI includes Zoho's documented global, organization, Europe, India, Australia, Japan, and China presets, all on port `993` with SSL/TLS.

- [Zoho IMAP configuration](https://www.zoho.com/mail/help/imap-access.html)
- [Zoho regional server table](https://www.zoho.com/mail/help/adminconsole/autodiscovery-settings.html)

### Outlook / Hotmail

Microsoft retired password and app-password IMAP login for Outlook.com accounts. Add the mailbox without a password, then select it and click **Connect Microsoft**.

The OAuth flow requires your own Microsoft Entra public-client Application ID:

1. Register an app that supports personal Microsoft accounts.
2. Enable public client flows.
3. Add delegated `https://outlook.office.com/IMAP.AccessAsUser.All` permission.
4. Paste the Application (client) ID into the app's Microsoft setup dialog.
5. Complete device-code sign-in once for each mailbox.

The app then uses OAuth2/XOAUTH2 with `outlook.office365.com:993`. Do not paste Microsoft account passwords into the import list.

- [Outlook.com IMAP settings](https://support.microsoft.com/en-us/outlook/pop-imap-and-smtp-settings-for-outlook-com)
- [Microsoft IMAP OAuth guide](https://learn.microsoft.com/en-us/exchange/client-developer/legacy-protocols/how-to-authenticate-an-imap-pop-smtp-application-by-using-oauth)
- [Register a Microsoft Entra application](https://learn.microsoft.com/en-us/entra/identity-platform/quickstart-register-app)

## Privacy

- Passwords and app passwords are never embedded in Python source.
- Mail accounts, including configured Yandex aliases, are encrypted with Windows DPAPI for the current Windows user.
- Writable encrypted state lives in `%LOCALAPPDATA%\Theara` and is never embedded in the application bundle.
- Saved accounts are local to that Windows user and device; they are not synchronized and the encrypted account file cannot normally be decrypted on another computer. Use **Paste list** or **Import file** once on each new device, which then saves its own encrypted copy.
- Microsoft MSAL refresh/access tokens are kept in a separate DPAPI-encrypted cache.
- Passwords, tokens, and upstream authentication responses are excluded from UI errors and logs.
- IMAP messages are fetched with `BODY.PEEK`, so checking for a code does not mark mail as read.
- The exact hour cutoff uses the IMAP server's receipt time when available; message bodies are downloaded in read-only batches only for rows inside the selected window.
- The Yandex delete action is separate from fetching, requires explicit row selection and the exact `DELETE ALL MAIL` confirmation, preserves folders and account settings, and permanently removes messages from every selectable mail folder.
- Decoded message bodies are kept in memory only, displayed as plain text, and size-limited for safety.
- Authenticator Base32 secrets and generated 2FA codes are never persisted; copying a generated code happens only when **Copy 2FA code** is clicked.
