terraform {
  required_version = ">= 1.8.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "= 5.100.0"
    }
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "= 5.0.1"
    }
    google = {
      source  = "hashicorp/google"
      version = "= 5.45.2"
    }
  }
}
