# Invisible Hand Arcade

A prototype web application for running a social-hour marketplace with dynamically priced minigames, snack redemptions, and live economic telemetry. The project uses Flask, SQLite, and Google OAuth to manage player balances, merchant tools, and an administrative command center.

## Features

- Google-based authentication for players, merchants, and admins.
- SQLite-backed models for users, transactions, products, inventory history, and head-to-head games.
- Single-player clicker prototype that awards credits and logs transactions.
- Two-player Prisoner's Dilemma matchmaking with automatic payouts based on player choices.
- Merchant console for creating items, updating prices or stock, and generating QR codes for checkout.
- Point-of-sale flow that merchants can use to process purchases, decrement stock, and transfer credits.
- Admin dashboard showing price trends, a live transaction ticker, and role assignment controls.

## Getting Started

### Requirements

- Python 3.10+
- Google Cloud OAuth 2.0 credentials (web application)

Install dependencies:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Configuration

Create an `.env` file or export the following variables before running the app:

```bash
export FLASK_APP=app.py
export FLASK_ENV=development
export SECRET_KEY="change-me"
export GOOGLE_CLIENT_ID="your-google-client-id"
export GOOGLE_CLIENT_SECRET="your-google-client-secret"
```

### Database

Initialize the SQLite database (this will create `instance/app.sqlite`):

```bash
flask --app app.py init-db
```

You can use the Flask shell to promote your account to admin or merchant after signing in once:

```bash
flask --app app.py shell
>>> from app import db
>>> from app.models import User, Role
>>> user = User.query.filter_by(email="you@example.com").first()
>>> user.role = Role.ADMIN
>>> db.session.commit()
```

### Running the App

Start the development server:

```bash
flask --app app.py run --debug
```

Visit <http://127.0.0.1:5000> to sign in and explore the prototype.

### QR Code Processing

When a buyer selects an item on the merchant page, the app generates a QR code encoding the product ID, buyer, and price. Merchants can scan the code (or open the processing URL) to complete the transaction and update stock levels.

### Tests

This prototype does not yet include automated tests. Please exercise the flows manually.
