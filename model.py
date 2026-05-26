import os
from google import genai

# Initialize the client
client = genai.Client(api_key="AIzaSyDTxftO4-Kvfb3rVD8vW6o7C3syV4hIe1o")

try:
    models = client.models.list()
    
    print("Available models:")
    for model in models:
        # Diagnostic: Print the model name and all its available attributes/methods
        print(f"\nModel Name: {model.name}")
        # Print the list of methods to find the correct attribute name
        print(f"Supported Methods: {getattr(model, 'supported_generation_methods', 'N/A')}")
        print(f"Methods (raw): {getattr(model, 'methods', 'N/A')}")
            
except Exception as e:
    print(f"Error accessing models: {e}")