# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]

### Added - Tasks Feature
- Task scheduling system using APScheduler
- Three interval types: Once, Daily, Weekly
- Task management UI (add, list, delete)
- Persistent task storage in SQLite
- Automatic task loading on bot startup
- Comprehensive documentation:
  - `TASKS_FEATURE.md` - Feature overview
  - `SETUP_TASKS.md` - Setup guide
  - `TASKS_ARCHITECTURE.md` - Technical architecture
  - `QUICK_START.md` - Quick reference
- Automated test suite (`test_tasks.py`)
- Full internationalization (English & Russian)

### Added - Security Improvements
- `.env.example` template file
- `SECURITY.md` comprehensive security guide
- Environment variable validation
- Better error messages for missing configuration
- Improved `.gitignore` for sensitive files
- Configuration module (`config.py`) with validation

### Added - Code Quality Improvements
- Type hints in main.py
- Better function documentation
- Structured logging with more context
- Proper database connection management
- Removed unnecessary lambda functions
- Better error handling in main()
- Graceful shutdown handling

### Added - Documentation
- Installation verification script (`verify_installation.sh`)
- Quick start guide
- Security best practices
- This changelog

### Changed
- Moved conversation persistence to `data/` directory
- Improved main.py structure with validation
- Better separation of concerns
- Enhanced logging throughout the application
- Cleaner conversation handler registration

### Fixed
- Global database connection handling
- Pickle file location (now in data/)
- Missing environment variable handling
- Better error messages for users

### Security
- Added `.env` to `.gitignore` (if not already)
- Added pickle files to `.gitignore`
- Documented security best practices
- Added configuration validation
- Improved error handling to avoid leaking sensitive info

## [Previous Version]

### Features
- Telegram bot integration
- Google Gemini AI chat
- Conversation history
- Image description
- Multi-language support (English, Russian)
- User authorization
- Persistent conversations

---

## Version Guidelines

This project follows [Semantic Versioning](https://semver.org/):
- MAJOR version for incompatible API changes
- MINOR version for new functionality in a backwards compatible manner
- PATCH version for backwards compatible bug fixes

## Categories

- **Added** for new features
- **Changed** for changes in existing functionality
- **Deprecated** for soon-to-be removed features
- **Removed** for now removed features
- **Fixed** for any bug fixes
- **Security** for vulnerability fixes
