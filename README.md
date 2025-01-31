# Slack Export Viewer

The Slack Export Viewer is a Python tool that processes a Slack export (ZIP) and generates a static HTML website, making it easier to browse and read archived Slack messages locally. It also downloads and links files (images and attachments) that were referenced in the exported data.

## How to Export Slack Data

To obtain the Slack export ZIP file, follow these steps:

1. **Open Slack Settings:**
   - Click on your workspace name in the top left corner.
   - Select **Tools & settings** from the dropdown menu.
   - Choose **Workspace settings**.

2. **Access Export Options:**
   - In the **Settings & Permissions** section, click on **Import/Export Data**.

3. **Export Data:**
   - Navigate to the **Export** tab.
   - Select the desired export date range (e.g., Entire history).
   - Click **Start Export** to generate the ZIP file.

Once the export is complete, download the ZIP file to your local machine.

## Installation

1. Clone or download this repository.
2. (Optional) Create and activate a Python virtual environment:
   ```bash
   python -m venv venv
   source venv/bin/activate  # Linux/macOS
   venv\Scripts\activate     # Windows
   ```
3. Install required dependencies:
   ```bash
   pip install -r requirements.txt
   ```
   
## Usage

Run the script with the ZIP file of your Slack export:

```bash
python slack_export_viewer.py sample.zip
```

This command will extract and process the Slack export, downloading any referenced Slack-hosted files. The output directory will be named "output" by default, containing:

- An "index.html" file with a summary of channels (in the "output" directory).
- A subdirectory for each Slack channel, containing:
  - "index.html" (HTML view)  
  - "index.txt" (text-only transcript)  
  - Possible "files" subdirectory with attachments  
  - CSV reports for missing or downloaded files  

### Supported Command-Line Arguments

• zip_file (positional): The path to your exported Slack ZIP.  
• -o, --output: The directory to place generated files (default: "output").  
• -channels <channel1 channel2 ...>: Only process specific channels.  
• -channels-existing: Instead of extracting from the ZIP, process channel directories already in the output folder.  
• -force-rewrite: Force regeneration of the HTML and text files, even if they already exist.  

Example:

```bash
python slack_export_viewer.py ./my_slack_export.zip -channels general random -o my_output
```

This command processes only the "general" and "random" channels from the zip and writes the results to "my_output".

## Generated Output Structure

Once the script finishes, your output directory might look like this:

```
output/
├─ index.html             <- Channel listing and stats
├─ general/
│  ├─ index.html          <- Main channel view
│  ├─ index.txt           <- Text transcript
│  ├─ files/
│  │  ├─ 12345-image.png  <- Downloaded image
│  │  └─ other files...
│  ├─ files_missing.csv   <- List of files that failed to download
│  └─ files_downloaded.csv<- List of successfully downloaded files
├─ random/
│  ├─ index.html
│  ├─ index.txt
│  └─ ...
└─ ...
```

## Contributing

Contributions are welcome! Feel free to open issues or submit pull requests for bug fixes or new features.

1. Fork this repository and clone it locally.  
2. Create a new branch for your feature or bug fix.  
3. Write tests (if applicable) and ensure all tests pass.  
4. Commit your changes and open a pull request.

## License

This project is released under the MIT License. See the [LICENSE](LICENSE) file for details.

## Additional Information

- Slack's export format is described here:  
  https://slack.com/help/articles/201658943-Export-your-workspace-data

- For general usage of Slack exports (e.g., who can export?), see Slack's help documents.

---

We hope you find this tool useful for archiving and reviewing Slack messages!