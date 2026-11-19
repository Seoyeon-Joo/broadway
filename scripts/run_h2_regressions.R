# 지표별 H2 회귀 (R / fixest). panel_for_R_*.csv 는 run_h2_regressions.py 가 만든 분석용 패널.
# 사용: Rscript scripts/run_h2_regressions.R [min_words]
library(fixest)
args <- commandArgs(trailingOnly = TRUE)
min_words <- if (length(args)) as.numeric(args[1]) else 0
out <- list()
for (med in c("hollywood", "broadway")) {
  d0 <- read.csv(sprintf("data/similarity/regressions/panel_for_R_%s.csv", med))
  meas <- setdiff(names(d0), c("work","year","month","week","lgross","lprice","lsize","lweeks","cc","video_id","transcript_words"))
  meas <- meas[!grepl("cov_ge|^lex_n_entities|n_sentences", meas)]
  for (m in meas) {
    d <- d0
    d[[m]][d$cc == 0] <- NA
    if (min_words > 0) d[[m]][d$cc == 1 & d$transcript_words < min_words] <- NA
    d <- d[(d$cc == 0 | !is.na(d[[m]])) & complete.cases(d[, c("lgross","lprice","lsize","lweeks")]), ]
    v <- d[[m]][d$cc == 1]
    if (length(unique(v)) < 3) next
    d$x <- ifelse(d$cc == 1, (d[[m]] - mean(v)) / sd(v), 0)
    fit <- feols(lgross ~ cc + lprice + lsize + lweeks + x | work + year + month + week, data = d, cluster = ~work)
    ct <- coeftable(fit)["x", ]
    out[[length(out) + 1]] <- data.frame(medium = med, measure = m, n_pairs = length(unique(d$video_id[d$cc == 1])),
      n_rows = nobs(fit), x = ct[1], se = ct[2], p = ct[4])
  }
}
res <- do.call(rbind, out); rownames(res) <- NULL
res$sig <- cut(res$p, c(0, .01, .05, .1, 1), labels = c("***", "**", "*", ""), right = FALSE)
print(res, digits = 3)
write.csv(res, sprintf("data/similarity/regressions/h2_by_measure_R_minwords%d.csv", min_words), row.names = FALSE)
