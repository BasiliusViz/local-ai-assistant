$QDRANT     = "http://localhost:6333"
$COLLECTION = "knowledge"

# Коллекция: dense (bge-m3, 1024) + sparse для гибридного поиска
$body = @{
    vectors = @{
        dense = @{ size = 1024; distance = "Cosine"; on_disk = $false }
    }
    sparse_vectors = @{
        sparse = @{ index = @{ on_disk = $false } }
    }
    optimizers_config = @{ default_segment_number = 2 }
}

Write-Host "Создаю коллекцию $COLLECTION..." -ForegroundColor Cyan
try {
    $r = Invoke-RestMethod -Method Put -Uri "$QDRANT/collections/$COLLECTION" -ContentType "application/json" -Body ($body | ConvertTo-Json -Depth 10)
    Write-Host "  OK: $($r.result)" -ForegroundColor Green
} catch {
    Write-Host "  Ошибка: $($_.Exception.Message)" -ForegroundColor Red
}

# Индексы для фильтрации:
# source     - источник (confluence, gitlab, jira, ...)
# space      - спейс/репозиторий/проект внутри источника
# acl_groups - права доступа, фильтр применяется внутри обхода HNSW
# source_id  - стабильный ID документа, для инкрементальной синхронизации
# updated_at - дата обновления
$fields = @(
    @{ name = "source";     type = "keyword"  },
    @{ name = "space";      type = "keyword"  },
    @{ name = "acl_groups"; type = "keyword"  },
    @{ name = "source_id";  type = "keyword"  },
    @{ name = "updated_at"; type = "datetime" }
)

Write-Host "`nИндексы:" -ForegroundColor Cyan
foreach ($f in $fields) {
    $idx = @{ field_name = $f.name; field_schema = $f.type }
    try {
        Invoke-RestMethod -Method Put -Uri "$QDRANT/collections/$COLLECTION/index" -ContentType "application/json" -Body ($idx | ConvertTo-Json) | Out-Null
        Write-Host "  OK  $($f.name)" -ForegroundColor Green
    } catch {
        Write-Host "  FAIL $($f.name): $($_.Exception.Message)" -ForegroundColor Red
    }
}

Write-Host "`nСостояние:" -ForegroundColor Cyan
$info = Invoke-RestMethod -Uri "$QDRANT/collections/$COLLECTION"
Write-Host "  статус:  $($info.result.status)"
Write-Host "  точек:   $($info.result.points_count)"
Write-Host "  векторы: $($info.result.config.params.vectors.PSObject.Properties.Name -join ', ')"
Write-Host "  sparse:  $($info.result.config.params.sparse_vectors.PSObject.Properties.Name -join ', ')"
Write-Host "  индексы: $($info.result.payload_schema.PSObject.Properties.Name -join ', ')"
