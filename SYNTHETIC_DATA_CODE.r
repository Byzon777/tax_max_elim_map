library(dplyr)
library(tidyr)
setwd("C:/Users/kchanwong/Documents/TAX_MAX_ELIM_MAP")
### Read in Synthetic ###
synth <- read.csv(gzfile("synthetic_puma_all_earners.csv.gz"))
# Prepare ALL_EARNERS for TAXSIM (usincometaxes): one person = one tax unit
# taxsim_results is keyed by taxsimid; join back to ALL_EARNERS if needed
PUMA_TAX <- synth |>
  group_by(PUMA, STATEFIP) |>
  summarise(TOTAL_POP = sum(PERWT),
            TOTAL_TAXMAX_POP  = sum(PERWT[INCWAGE >= 168600]),
            MEAN_HIGHINCOME = weighted.mean(INCWAGE[INCWAGE >= 168600], w = PERWT[INCWAGE >= 168600]),
            TAX_RATE = weighted.mean(PERC_TAX[INCWAGE >= 168600], w = PERWT[INCWAGE >= 168600]),
            TAX_RATE_CF = weighted.mean(TAX_RATE_COUNTERFACTUAL[INCWAGE >= 168600]),
            w = sum(PERWT[INCWAGE >= 168600]),
    .groups = "drop"
  )
###
PUMA_TO_CD <- read.csv(
  "geocorr2022_2619606869.csv"
) |>
  as_tibble() |>
  transmute(
    PUMA    = as.integer(puma22),
    STATEFIP = as.integer(state),
    CD      = cd119,
    FACTOR  = afact
  )
##
BEA_FIPS <- read.csv("BEA_FIPS.csv") |>
  as_tibble() |>
  transmute(
    county    = as.integer(BEA.FIPS),
    real_fips = as.integer(FIPS)
  )
COUNTY_GDP <-   read.csv(
  "Table (4).csv"
  ) |>  as_tibble() |>
  mutate(county = as.integer(GeoFIPS),
         X2023 = as.integer(X2024)) |>
  rename(GDP = X2023) |>
  na.omit() |>
  left_join(BEA_FIPS, by = "county") |>
  mutate(county = coalesce(real_fips, county)) |>
  select(-real_fips)
# Apportion each old county's GDP across its overlapping planning region(s)
# Join on PUMA *and* STATEFIP (PUMA codes repeat across states), then
# apportion each PUMA's tax to its CDs by the population allocation factor
CD_TAX <- PUMA_TAX |>
  mutate(PUMA = as.integer(PUMA), STATEFIP = as.integer(STATEFIP)) |>
  inner_join(PUMA_TO_CD, by = c("PUMA", "STATEFIP")) |>
  group_by(STATEFIP, CD) |>
  summarise(TOTAL_POP = round(sum(TOTAL_POP * FACTOR)),
            TOTAL_TAXMAX_POP = round(sum(TOTAL_TAXMAX_POP * FACTOR)),
            TAX_RATE = weighted.mean(TAX_RATE, w = TOTAL_POP * FACTOR),
            TAX_RATE_CF = weighted.mean(TAX_RATE_CF, w = TOTAL_POP * FACTOR),
             .groups = "drop") 
# state abbreviation -> FIPS, derived from the same geocorr file used for PUMA_TO_CD
STATE_ABB_TO_FIPS <- read.csv(
  "geocorr2022_2619606869.csv"
) |>
  as_tibble() |>
  distinct(stab, STATEFIP = as.integer(state))
legislators <- read.csv("legislators-current.csv") |>
  as_tibble() |>
  filter(type == "rep") |>
  select(full_name, party, stab = state, CD = district) |>
  mutate(CD = if_else(stab == "DC", 98L, CD)) |>  # geocorr codes DC's non-voting seat as cd119 = 98, not 0
  inner_join(STATE_ABB_TO_FIPS, by = "stab")
CD_TAX_LEGISLATORS <- CD_TAX |>
  left_join(legislators, by = c("CD", "STATEFIP")) |>
  mutate(full_name = ifelse(is.na(full_name), "Vacant", full_name)) |>
  mutate(GEOID = sprintf("%02d%02d", STATEFIP, CD))
