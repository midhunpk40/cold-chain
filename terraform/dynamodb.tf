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
locals {
  # product limits: temperature C, humidity %, sustain = bad readings in a row to open,
  # recover = good readings in a row to end the excursion
  seed_products = {
    "VAC-2-8C" = { name = "Vaccine 2-8 C", temp_min = 2, temp_max = 8, hum_min = 35, hum_max = 65, sustain = 3, recover = 3 }
    "INS-2-8C" = { name = "Insulin 2-8 C", temp_min = 2, temp_max = 8, hum_min = 30, hum_max = 70, sustain = 2, recover = 3 }
  }
}

resource "aws_dynamodb_table" "products" {
  name         = "coldchain-products"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "product_id"

  attribute {
    name = "product_id"
    type = "S"
  }

  point_in_time_recovery {
    enabled = true
  }
}

# N = number type in DynamoDB JSON (value is passed as text)
resource "aws_dynamodb_table_item" "product" {
  for_each   = local.seed_products
  table_name = aws_dynamodb_table.products.name
  hash_key   = aws_dynamodb_table.products.hash_key

  item = jsonencode({
    product_id    = { S = each.key }
    name          = { S = each.value.name }
    temp_min      = { N = tostring(each.value.temp_min) }
    temp_max      = { N = tostring(each.value.temp_max) }
    hum_min       = { N = tostring(each.value.hum_min) }
    hum_max       = { N = tostring(each.value.hum_max) }
    sustain_count = { N = tostring(each.value.sustain) }
    recover_count = { N = tostring(each.value.recover) }
  })
}

resource "aws_dynamodb_table" "excursions" {
  name         = "coldchain-excursions"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "excursion_id"

  attribute {
    name = "excursion_id"
    type = "S"
  }

  # attributes used by an index must be declared here
  attribute {
    name = "shipment_id"
    type = "S"
  }

  attribute {
    name = "started_at"
    type = "S"
  }

  attribute {
    name = "workflow_status"
    type = "S"
  }

  # "all excursions of one shipment, newest last"
  global_secondary_index {
    name            = "by_shipment"
    hash_key        = "shipment_id"
    range_key       = "started_at"
    projection_type = "ALL"
  }

  # "all OPEN excursions"
  global_secondary_index {
    name            = "by_status"
    hash_key        = "workflow_status"
    range_key       = "started_at"
    projection_type = "ALL"
  }

  point_in_time_recovery {
    enabled = true
  }
}
