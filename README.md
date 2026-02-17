# GeminiBot - Personalized Telegram Bot

GeminiBot is a Telegram bot tailored to chat with Google's Gemini AI chatbot. Leveraging the official Gemini Bot on the Telegram platform, it engages users in dynamic conversations.

[Set-up Tutorial on Medium](https://medium.com/@alirezafathi/how-to-use-google-gemini-ai-in-your-personal-telegram-bot-on-your-own-server-b1f0b9de2bdd)

## Getting Started

### Prerequisites

Before deploying the bot, ensure you have the following:

- Python 3.10 installed on your system
- Obtain a [Telegram API token](https://core.telegram.org/bots) from BotFather
- Acquire a [Gemini API key](https://makersuite.google.com/app/apikey) from the Google Gemini website
- Get your Telegram Account id from [Show Json Bot](https://t.me/ShowJsonBot). Account id is different than Account username and you should set it in `.env` file to restrict GeminiBot to your account.

##


https://github.com/sudoAlireza/GeminiBot/assets/87416117/beeb0fd2-73c6-4631-baea-2e3e3eeb9319



### Installation

1. Clone the repository:

   ```bash
   git clone https://github.com/sudoAlireza/GeminiBot.git
   ```

2. Navigate to the project directory:

   ```bash
   cd GeminiBot
   ```

3. Install the required dependencies:

   ```bash
   pip install -r requirements.txt
   ```

### Environment Variables

The bot is configured using environment variables. The following variables are required:

-   `TELEGRAM_BOT_TOKEN`: Your Telegram bot token from BotFather.
-   `GEMINI_API_TOKEN`: Your Gemini API key from Google AI Studio.
-   `GEMINI_MODEL`: The Gemini model to use (e.g., `gemini-flash-latest`).
-   `AUTHORIZED_USER`: A comma-separated list of your Telegram user IDs to restrict bot access.
-   `LANGUAGE`: The language code for the bot's interface (e.g., `en`, `ru`). Defaults to `ru`.
-   `LOG_LEVEL`: The logging level for the bot (e.g., `DEBUG`, `INFO`, `WARNING`, `ERROR`). Defaults to `INFO`.

### Local Development

For local development, it's recommended to use a Python virtual environment to manage dependencies.

**Steps:**

1.  **Create and activate a virtual environment:**

    ```bash
    python3 -m venv venv
    source venv/bin/activate
    ```

2.  **Install dependencies:**

    ```bash
    pip install -r requirements.txt
    ```

3.  **Set environment variables:** Set the required environment variables in your active shell session. Replace the placeholder values with your actual tokens and IDs.

    ```bash
    export TELEGRAM_BOT_TOKEN=<Your Telegram Bot Token>
    export GEMINI_API_TOKEN=<Your Gemini API key>
    export GEMINI_MODEL=<Your Gemini Model> # e.g., gemini-flash-latest
    export AUTHORIZED_USER="<your_user_id_1>,<your_user_id_2>"
    export LANGUAGE=ru # Set the desired language (e.g., en, ru)
    ```

4.  **Run the bot:**

    ```bash
    python main.py
    ```

### Deployment with Docker

The recommended way to deploy the bot is using Docker and Docker Compose.

**Data Persistence:**

The bot stores conversation data in the `data` directory. This directory is mounted as a volume in the Docker Compose configuration to ensure that your data is preserved even if the container is removed.

**Configuration:**

When deploying with Docker, you should provide the required environment variables to the container. The recommended way to do this is through your orchestration platform (e.g., Portainer, Kubernetes) by setting the environment variables in the container's configuration.

**Build and Run:**

```bash
docker-compose up -d --build
```

The bot will now be running in the background. To view the logs, you can use the following command:

```bash
docker-compose logs -f
```

To stop the bot, use the following command:

```bash
docker-compose down
```

## Features

- Engage in online conversations with Google's Gemini AI chatbot
- Maintain conversation history for continuing or initiating new discussions
- Send images with captions to receive responses based on the image content. For example, the bot can read text within images and convert it to text.
- **Schedule automated tasks** to send prompts to Gemini at specific times with customizable intervals (once, daily, weekly) - [Learn more](TASKS_FEATURE.md)

## Internationalization (i18n)

The bot supports multiple languages using `gettext` and `Babel`.

### Adding a New Language

To add a new language (e.g., Spanish - `es`):

1.  **Initialize the new language catalog:**

    ```bash
    venv/bin/pybabel init -i locales/messages.pot -d locales -l es
    ```

2.  **Translate the strings:** Edit the newly created `locales/es/LC_MESSAGES/messages.po` file and translate the `msgid` strings into Spanish.

3.  **Compile the translations:**

    ```bash
    venv/bin/pybabel compile -d locales
    ```

### Updating Existing Translations

If you add new translatable strings to the code:

1.  **Extract new strings to the POT file:**

    ```bash
    venv/bin/pybabel extract -F babel.cfg -o locales/messages.pot .
    ```

2.  **Update existing language catalogs:**

    ```bash
    venv/bin/pybabel update -i locales/messages.pot -d locales -l en # For English
    venv/bin/pybabel update -i locales/messages.pot -d locales -l es # For Spanish (or other languages)
    ```

3.  **Translate new strings:** Edit the `.po` files for each language and translate the new `msgid` entries.

4.  **Compile the translations:**

    ```bash
    venv/bin/pybabel compile -d locales
    ```

## To-Do

- [x] **Removing Specific Conversation from History**
- [ ] **Add Conversation Feature to Images Part**
- [ ] **Handle Long Responses in Multiple Messages**
- [ ] **Add Tests and Easy Deployment**


## Documentation

For detailed instructions on using Telegram bots, refer to the [Telegram Bots Documentation](https://core.telegram.org/bots).

To begin with Gemini, refer to the [Gemini API: Quickstart with Python](https://ai.google.dev/tutorials/python_quickstart).


## Security

Ensure the security of your API keys and sensitive information. Follow best practices for securing API keys and tokens.

## Contributing

Contributions to GeminiBot are encouraged. Feel free to submit issues and pull requests.
