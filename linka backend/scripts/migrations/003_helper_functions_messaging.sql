-- Helper functions for messaging and notifications

-- Get user conversations with details
CREATE OR REPLACE FUNCTION get_user_conversations(
  p_user_id UUID,
  p_archived BOOLEAN DEFAULT false,
  p_limit INTEGER DEFAULT 20,
  p_offset INTEGER DEFAULT 0
)
RETURNS TABLE (
  id UUID,
  participant_1 UUID,
  participant_2 UUID,
  other_user_id UUID,
  other_user_name TEXT,
  other_user_avatar TEXT,
  last_message_at TIMESTAMPTZ,
  last_message_preview TEXT,
  order_id UUID,
  product_id UUID,
  created_at TIMESTAMPTZ
) AS $$
BEGIN
  RETURN QUERY
  SELECT 
    c.id,
    c.participant_1,
    c.participant_2,
    CASE 
      WHEN c.participant_1 = p_user_id THEN c.participant_2
      ELSE c.participant_1
    END AS other_user_id,
    CASE 
      WHEN c.participant_1 = p_user_id THEN up2.full_name
      ELSE up1.full_name
    END AS other_user_name,
    CASE 
      WHEN c.participant_1 = p_user_id THEN up2.avatar_url
      ELSE up1.avatar_url
    END AS other_user_avatar,
    c.last_message_at,
    c.last_message_preview,
    c.order_id,
    c.product_id,
    c.created_at
  FROM public.conversations c
  LEFT JOIN public.user_profiles up1 ON c.participant_1 = up1.id
  LEFT JOIN public.user_profiles up2 ON c.participant_2 = up2.id
  WHERE (c.participant_1 = p_user_id OR c.participant_2 = p_user_id)
    AND c.is_archived = p_archived
  ORDER BY c.last_message_at DESC NULLS LAST
  LIMIT p_limit OFFSET p_offset;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- Mark conversation as read
CREATE OR REPLACE FUNCTION mark_conversation_read(
  p_conversation_id UUID,
  p_user_id UUID
)
RETURNS JSONB AS $$
DECLARE
  v_count INTEGER;
BEGIN
  UPDATE public.messages
  SET is_read = true, read_at = NOW()
  WHERE conversation_id = p_conversation_id
    AND sender_id != p_user_id
    AND is_read = false;
  
  GET DIAGNOSTICS v_count = ROW_COUNT;
  
  RETURN jsonb_build_object('success', true, 'count', v_count);
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- Get retailer sales summary
CREATE OR REPLACE FUNCTION get_retailer_sales_summary(
  p_retailer_id UUID,
  p_days INTEGER DEFAULT 7
)
RETURNS JSONB AS $$
DECLARE
  v_result JSONB;
BEGIN
  SELECT jsonb_build_object(
    'total_orders', COUNT(DISTINCT o.id),
    'total_revenue', COALESCE(SUM(o.total_amount), 0),
    'total_items_sold', COALESCE(SUM(oi.quantity), 0),
    'average_order_value', COALESCE(AVG(o.total_amount), 0)
  )
  INTO v_result
  FROM public.orders o
  LEFT JOIN public.order_items oi ON o.id = oi.order_id
  WHERE o.retailer_id = p_retailer_id
    AND o.created_at >= NOW() - (p_days || ' days')::INTERVAL
    AND o.status NOT IN ('cancelled');
  
  RETURN v_result;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- Mark all notifications as read for user
CREATE OR REPLACE FUNCTION mark_all_notifications_read(
  p_user_id UUID
)
RETURNS JSONB AS $$
DECLARE
  v_count INTEGER;
BEGIN
  UPDATE public.notifications
  SET is_read = true, read_at = NOW()
  WHERE user_id = p_user_id AND is_read = false;
  
  GET DIAGNOSTICS v_count = ROW_COUNT;
  
  RETURN jsonb_build_object('success', true, 'count', v_count);
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;
