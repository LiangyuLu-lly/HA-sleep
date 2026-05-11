#!/bin/bash

echo "========================================"
echo "CNN-BiLSTM Sleep Algorithm Setup"
echo "========================================"
echo

echo "Creating virtual environment..."
python3 -m venv venv
if [ $? -ne 0 ]; then
    echo "Error: Failed to create virtual environment"
    exit 1
fi

echo
echo "Activating virtual environment..."
source venv/bin/activate

echo
echo "Installing dependencies..."
pip install -r requirements.txt
if [ $? -ne 0 ]; then
    echo "Error: Failed to install dependencies"
    exit 1
fi

echo
echo "========================================"
echo "Setup completed successfully!"
echo "========================================"
echo
echo "To activate the virtual environment, run:"
echo "  source venv/bin/activate"
echo
echo "To run tests, use:"
echo "  pytest tests/"
echo
