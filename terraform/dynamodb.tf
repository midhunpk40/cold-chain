locals {
  # demo shipments: which device rides with which shipment and product
  seed_shipments = {
    "SHP-1001" = { device = "coldchain-sensor-01", product = "VAC-2-8C", origin = "Mumbai Cold Store", destination = "Kochi Hospital" }
    "SHP-1002" = { device = "coldchain-sensor-02", product = "INS-2-8C", origin = "Chennai Depot", destination = "Madurai Clinic" }
  }
}

resource "aws_dynamodb_table" "shipments" {
  name         = "coldchain-shipments"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "shipment_id"

  attribute {
    name = "shipment_id"
    type = "S"
  }

  point_in_time_recovery {
    enabled = true
  }
}

resource "aws_dynamodb_table" "readings" {
  name         = "coldchain-readings"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "shipment_id"
  range_key    = "ts_id" # timestamp#message_id: sorted by time, unique per message

  attribute {
    name = "shipment_id"
    type = "S"
  }

  attribute {
    name = "ts_id"
    type = "S"
  }

  # auto-delete old readings (attribute holds epoch seconds)
  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }

  point_in_time_recovery {
    enabled = true
  }
}

resource "aws_dynamodb_table" "sensor_state" {
  name         = "coldchain-sensor-state"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "device_id"

  attribute {
    name = "device_id"
    type = "S"
  }

  point_in_time_recovery {
    enabled = true
  }
}

# seed rows. item format = DynamoDB JSON, S means string
resource "aws_dynamodb_table_item" "shipment" {
  for_each   = local.seed_shipments
  table_name = aws_dynamodb_table.shipments.name
  hash_key   = aws_dynamodb_table.shipments.hash_key

  item = jsonencode({
    shipment_id  = { S = each.key }
    device_thing = { S = each.value.device }
    product_id   = { S = each.value.product }
    origin       = { S = each.value.origin }
    destination  = { S = each.value.destination }
    status       = { S = "IN_TRANSIT" }
  })
}
